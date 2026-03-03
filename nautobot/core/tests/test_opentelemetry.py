from django.urls import reverse
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nautobot.core import testing


class APITraceGenerationTest(testing.APITestCase):
    """Verify that OpenTelemetry spans are generated when an API endpoint is called."""

    def setUp(self):
        super().setUp()
        self._exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        # Do not override the global TracerProvider - once Nautobot startup sets a real provider,
        # trace.set_tracer_provider() is permanently blocked. Instead, pass tracer_provider
        # directly to instrument() so _DjangoMiddleware._tracer uses our test exporter.
        #
        # Call uninstrument() first: if Nautobot startup already called instrument() with the
        # production provider, a bare instrument() call would be a no-op and _DjangoMiddleware._tracer
        # would keep pointing at the production exporter instead of our InMemorySpanExporter.
        DjangoInstrumentor().uninstrument()
        DjangoInstrumentor().instrument(tracer_provider=self._provider)
        # DjangoInstrumentor prepends its middleware to settings.MIDDLEWARE, but
        # self.client.handler has already loaded its chain via super().setUp().
        # Force a reload so the OTEL middleware is actually active for this test.
        self.client.handler.load_middleware()

    def tearDown(self):
        DjangoInstrumentor().uninstrument()
        self.client.handler.load_middleware()
        super().tearDown()

    def test_api_request_generates_span(self):
        """A GET request to /api/status/ should produce at least one span with HTTP 200."""
        url = reverse("api-status")
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, 200)

        spans = self._exporter.get_finished_spans()
        self.assertGreater(len(spans), 0, "Expected at least one span to be exported")

        http_span = next(
            (s for s in spans if s.attributes.get("http.status_code") == 200),
            None,
        )
        if http_span is None:
            self.fail("Expected a span with http.status_code=200")
        # http.target (old semconv) or url.path (new semconv) or http.url (test client fallback:
        # the Django test client does not set RAW_URI/REQUEST_URI in the WSGI environ, so
        # the OTEL WSGI library sets http.url with the full URL instead of http.target)
        path = (
            http_span.attributes.get("http.target")
            or http_span.attributes.get("url.path")
            or http_span.attributes.get("http.url", "")
        )
        self.assertIn(url, path, "Expected span to contain the request path")
