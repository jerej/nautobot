import logging
import threading
import time

from django.conf import settings
from django.core import cache
from django.core.wsgi import get_wsgi_application
from django.db import connections

import nautobot

logger = logging.getLogger(__name__)


def _warm_graphql_schema(worker_id=None):
    """Build the GraphQL schema so the first GraphQL request doesn't have to.

    The schema is generated lazily on first access of `graphene_settings.SCHEMA` (a dotted path to
    `nautobot.core.graphql.schema_init.schema`, which builds the schema at import time). On large
    installations that build can take tens of seconds, and it is paid by whichever request happens to
    touch GraphQL first in each worker process. Touching it here moves that cost to worker startup.

    Any exception is logged and swallowed: a failure to pre-build the schema must never prevent the
    worker from serving traffic, as the schema will simply be built lazily as before.
    """
    try:
        from graphene_django.settings import graphene_settings

        start = time.monotonic()
        logger.info("Starting GraphQL schema warmup on worker %s ...", worker_id)
        # Accessing `SCHEMA` is not a no-op: the setting is a dotted path to
        # `nautobot.core.graphql.schema_init.schema`, and that module generates and builds the schema
        # at import time. Reading the attribute here performs that import, and `graphene_settings`
        # memoizes the result, so the first GraphQL request finds it already built.
        # Deliberately does not touch `.graphql_schema`, which only exists in graphene 3.x.
        graphene_settings.SCHEMA  # pylint: disable=pointless-statement
        logger.info(
            "GraphQL schema warmup complete on worker %s in %.2f ms",
            worker_id,
            (time.monotonic() - start) * 1000,
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("GraphQL schema warmup failed on worker %s; schema will be built on demand", worker_id)


# This is the Django default left here for visibility on how the Nautobot pattern
# differs.
# os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nautobot.core.settings")

# Instead of just pointing to `DJANGO_SETTINGS_MODULE` and letting Django run with it,
# we're using the custom Nautobot loader code to read environment or config path for us.
nautobot.setup()

# Use try/except because we might not be running uWSGI. If `settings.WEBSERVER_WARMUP` is `True`,
# will first call `get_internal_wsgi_application` which does not have `uwsgi` module loaded
# already. Therefore, `settings.WEBSERVER_WARMUP` to `False` for this code to be loaded.
try:
    import uwsgidecorators

    @uwsgidecorators.postfork
    def fix_uwsgi():
        import uwsgi

        logger.info("Closing existing DB and cache connections on worker %s after uWSGI forked ...", uwsgi.worker_id())
        connections.close_all()
        cache.close_caches()

        # Pre-build the GraphQL schema in the background so the first GraphQL request doesn't pay for
        # it. This runs *after* the connections above are closed, so the thread opens its own. It is a
        # daemon thread so it can never delay worker shutdown, and the worker starts serving traffic
        # immediately rather than waiting for the schema.
        if settings.GRAPHQL_SCHEMA_WARMUP:
            threading.Thread(
                target=_warm_graphql_schema,
                kwargs={"worker_id": uwsgi.worker_id()},
                name="graphql-schema-warmup",
                daemon=True,
            ).start()

except ImportError:
    pass

application = get_wsgi_application()
