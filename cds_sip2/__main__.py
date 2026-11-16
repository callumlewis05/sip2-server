"""Composition root: ``python -m cds_sip2``.

The only place where the packages meet: settings are read, the ILS client
is built and handed to the SIP2 side as an ``ILSBackend``.
"""

import asyncio
import logging
import signal
import sys

from . import logging as log
from .config import ConfigError, Settings
from .health import HealthServer, ILSProbe
from .ils import ILSClient
from .sip2.dispatcher import Dispatcher
from .sip2.handlers import Handlers
from .sip2.server import Sip2Server

logger = logging.getLogger("cds_sip2")


async def serve(settings, stop=None):
    """Run until ``stop`` is set (or SIGTERM / SIGINT), then shut down cleanly."""
    ils = ILSClient(
        settings.ils_url,
        settings.ils_token,
        settings.ils_connect_timeout,
        settings.ils_read_timeout,
    )
    server = Sip2Server(Dispatcher(Handlers(ils, settings), settings), settings)
    health = HealthServer(server, ILSProbe(ils, settings.health_ils_cache), settings)

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await server.start()
        await health.start()
        await stop.wait()
        logger.info("shutting down", extra={"event": "server.shutdown"})
    finally:
        await server.shutdown()
        await health.close()
        await ils.aclose()


def main():
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"cds-sip2: configuration error: {exc}", file=sys.stderr)
        return 2
    log.configure(settings.log_level, settings.log_hash_key)
    if not settings.ils_token:
        logger.warning(
            "CDS_SIP2_ILS_TOKEN is empty: every ILS call will be rejected",
            extra={"event": "config", "alert": True},
        )
    asyncio.run(serve(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
