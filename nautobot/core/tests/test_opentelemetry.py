"""Tests for OpenTelemetry instrumentation in Nautobot."""

from unittest.mock import MagicMock, patch

import requests
from opentelemetry import trace
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

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
