"""Tests for OpenTelemetry instrumentation in Nautobot."""

import json
from unittest.mock import MagicMock, patch

import requests
import structlog
from django.test import RequestFactory
from opentelemetry import trace
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from nautobot.core.middleware import GraphQLOpenTelemetryMiddleware
from nautobot.core.testing import TestCase


class RequestsInstrumentationTraceparentTest(TestCase):
    """Verify that OpenTelemetry Requests instrumentation injects the traceparent header into outgoing HTTP requests."""

    def setUp(self):
        super().setUp()

        # Uninstrument first in case requests was already instrumented during app startup
        RequestsInstrumentor().uninstrument()

        # Set up an isolated in-memory tracer provider
        self._exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))

        # Swap in the test provider and W3C TraceContext propagator
        self._original_provider = trace.get_tracer_provider()
        self._original_propagator = get_global_textmap()
        trace.set_tracer_provider(self._provider)
        set_global_textmap(TraceContextTextMapPropagator())

        # Instrument requests with the test tracer provider
        self._instrumentor = RequestsInstrumentor()
        self._instrumentor.instrument(tracer_provider=self._provider)

    def tearDown(self):
        self._instrumentor.uninstrument()
        trace.set_tracer_provider(self._original_provider)
        set_global_textmap(self._original_propagator)
        super().tearDown()

    def test_traceparent_header_injected_in_outgoing_requests(self):
        """Outgoing HTTP requests made within an active span must include the W3C traceparent header.

        RequestsInstrumentor wraps requests.Session.send and uses the active propagator to inject
        trace context into the PreparedRequest headers before the request hits the network.
        Patching at the HTTPAdapter level lets the instrumentation wrapper run in full while
        still capturing the headers that would have been sent on the wire.
        """
        captured_headers = {}

        def mock_adapter_send(self_adapter, request, **kwargs):
            captured_headers.update(request.headers)
            response = MagicMock()
            response.status_code = 200
            response.headers = {}
            response.history = []
            response.is_redirect = False
            response.content = b""
            return response

        tracer = self._provider.get_tracer(__name__)

        with patch("requests.adapters.HTTPAdapter.send", mock_adapter_send):
            with tracer.start_as_current_span("test-parent-span"):
                requests.get("https://example.com/api/test")

        self.assertIn(
            "traceparent",
            captured_headers,
            "The traceparent header was not injected into the outgoing HTTP request.",
        )

        # Validate the W3C Trace Context format: version-traceid-parentid-flags
        traceparent = captured_headers["traceparent"]
        parts = traceparent.split("-")
        self.assertEqual(
            len(parts),
            4,
            f"traceparent header has unexpected format (expected version-traceid-parentid-flags): {traceparent!r}",
        )
        self.assertEqual(parts[0], "00", f"traceparent version should be '00', got: {parts[0]!r}")
        self.assertEqual(
            len(parts[1]),
            32,
            f"traceparent trace-id should be 32 lowercase hex chars, got: {parts[1]!r}",
        )
        self.assertEqual(
            len(parts[2]),
            16,
            f"traceparent parent-id should be 16 lowercase hex chars, got: {parts[2]!r}",
        )


class GraphQLOpenTelemetryMiddlewareTest(TestCase):
    """Verify GraphQLOpenTelemetryMiddleware emits correct OTel spans and structured log entries."""

    _SAMPLE_QUERY = "query GetSites { sites { id name } }"
    _SAMPLE_VARIABLES = {"limit": 10}

    def setUp(self):
        super().setUp()
        self._exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._original_provider = trace.get_tracer_provider()
        trace.set_tracer_provider(self._provider)

    def tearDown(self):
        trace.set_tracer_provider(self._original_provider)
        super().tearDown()

    def _make_middleware(self, status_code=200):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        return GraphQLOpenTelemetryMiddleware(MagicMock(return_value=mock_response))

    def _build_request(self, path="/api/graphql", query=None, variables=None, xff="203.0.113.5, 10.0.0.1"):
        """Return a POST WSGIRequest pre-populated with a JSON GraphQL body."""
        if query is None:
            query = self._SAMPLE_QUERY
        body = {"query": query}
        if variables is not None:
            body["variables"] = variables
        request = RequestFactory().post(path, data=json.dumps(body), content_type="application/json")
        request.user = self.user
        if xff:
            request.META["HTTP_X_FORWARDED_FOR"] = xff
        return request

    def test_span_created_with_correct_attributes(self):
        """A GraphQL request must produce a span named after the operation type with all expected attributes."""
        middleware = self._make_middleware(status_code=200)
        request = self._build_request(variables=self._SAMPLE_VARIABLES, xff="203.0.113.5, 10.0.0.1")

        middleware(request)

        spans = self._exporter.get_finished_spans()
        self.assertEqual(len(spans), 1, "Expected exactly one span to be emitted for a GraphQL request.")
        span = spans[0]

        self.assertEqual(span.name, "graphql query")

        attrs = span.attributes
        self.assertEqual(attrs.get("enduser.id"), self.user.username)
        self.assertEqual(attrs.get("http.client_ip"), "203.0.113.5", "Should use the leftmost X-Forwarded-For entry.")
        self.assertEqual(attrs.get("graphql.document"), self._SAMPLE_QUERY)
        self.assertEqual(attrs.get("graphql.variables"), json.dumps(self._SAMPLE_VARIABLES))
        self.assertEqual(attrs.get("graphql.operation.type"), "query")
        self.assertEqual(attrs.get("http.status_code"), 200)

    def test_log_emitted_with_correct_fields(self):
        """The INFO log for a GraphQL request must include username, IP, query, variables, status, and duration."""
        middleware = self._make_middleware(status_code=200)
        request = self._build_request(variables=self._SAMPLE_VARIABLES, xff="203.0.113.5")

        with structlog.testing.capture_logs() as captured:
            middleware(request)

        graphql_logs = [e for e in captured if e.get("event") == "graphql.request"]
        self.assertEqual(len(graphql_logs), 1, f"Expected exactly one graphql.request log entry; got: {captured!r}")
        log = graphql_logs[0]

        self.assertEqual(log.get("log_level"), "info")
        self.assertEqual(log.get("username"), self.user.username)
        self.assertEqual(log.get("client_ip"), "203.0.113.5")
        self.assertEqual(log.get("query"), self._SAMPLE_QUERY)
        self.assertEqual(log.get("variables"), self._SAMPLE_VARIABLES)
        self.assertEqual(log.get("http_status"), 200)
        self.assertIn("duration_ms", log, "duration_ms must be present in the log entry.")
        self.assertIsInstance(log["duration_ms"], float)
        self.assertGreaterEqual(log["duration_ms"], 0.0)
