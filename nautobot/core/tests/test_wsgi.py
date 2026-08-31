from unittest import mock

from django.conf import settings
from django.test import TestCase

from nautobot.core.wsgi import _warm_graphql_schema


class GraphQLSchemaWarmupTest(TestCase):
    """Tests for the GraphQL schema warmup performed by the uWSGI postfork hook."""

    def test_setting_defaults_to_enabled(self):
        """GRAPHQL_SCHEMA_WARMUP should exist and default to True."""
        self.assertTrue(settings.GRAPHQL_SCHEMA_WARMUP)

    def test_warmup_builds_the_schema(self):
        """The warmup should read `graphene_settings.SCHEMA`, which imports and builds the schema."""
        # `SCHEMA` is resolved lazily by graphene_django, so use PropertyMock to observe that it is read.
        schema_property = mock.PropertyMock(return_value=mock.Mock())
        with mock.patch("graphene_django.settings.graphene_settings") as mock_graphene_settings:
            type(mock_graphene_settings).SCHEMA = schema_property
            with self.assertLogs("nautobot.core.wsgi", level="INFO") as logs:
                _warm_graphql_schema(worker_id=1)

            # Reading `SCHEMA` is what imports `schema_init` and therefore builds the schema.
            schema_property.assert_called_once()

        self.assertTrue(any("Starting GraphQL schema warmup" in line for line in logs.output))
        self.assertTrue(any("GraphQL schema warmup complete" in line for line in logs.output))

    def test_warmup_does_not_use_graphene_3_only_api(self):
        """Regression test: `.graphql_schema` does not exist in graphene 2.x, which ltm-2.4 pins.

        Reading it raises `AttributeError: Type "graphql_schema" not found in the Schema`, which made
        the warmup fail on every worker while still (correctly) letting them serve traffic.
        """
        schema = mock.Mock()
        # Mimic graphene 2.x, where accessing an unknown attribute on a Schema raises.
        type(schema).graphql_schema = mock.PropertyMock(
            side_effect=AttributeError('Type "graphql_schema" not found in the Schema')
        )
        with mock.patch("graphene_django.settings.graphene_settings") as mock_graphene_settings:
            type(mock_graphene_settings).SCHEMA = mock.PropertyMock(return_value=schema)
            with self.assertLogs("nautobot.core.wsgi", level="INFO") as logs:
                _warm_graphql_schema(worker_id=1)

        # Must have succeeded, i.e. never touched the graphene-3-only attribute.
        self.assertTrue(any("GraphQL schema warmup complete" in line for line in logs.output))
        self.assertFalse(any("GraphQL schema warmup failed" in line for line in logs.output))

    def test_warmup_failure_is_logged_and_suppressed(self):
        """A failure to build the schema must be logged but must never propagate out of the worker hook."""
        with mock.patch("graphene_django.settings.graphene_settings") as mock_graphene_settings:
            type(mock_graphene_settings).SCHEMA = mock.PropertyMock(side_effect=RuntimeError("boom"))
            with self.assertLogs("nautobot.core.wsgi", level="ERROR") as logs:
                # Must not raise.
                _warm_graphql_schema(worker_id=2)

        self.assertTrue(any("GraphQL schema warmup failed" in line for line in logs.output))
        self.assertTrue(any("boom" in line for line in logs.output))
