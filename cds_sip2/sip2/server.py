"""asyncio TCP server: one task and one :class:`Session` per connection.

Limits: a maximum connection count, an idle timeout per frame, a maximum
frame size, and TCP keepalive. Per-IP limits belong at the edge: behind
stunnel every connection comes from loopback.

Shutdown (SIGTERM): stop accepting, close idle connections at once, give
connections in the middle of a request ``shutdown_grace`` seconds to finish
so that deploys do not turn checkouts into ambiguous outcomes, then cancel
whatever is left.
"""

import asyncio
import logging
import socket

from .framing import ConnectionClosed, FrameTooLarge, read_frame
from .session import Session

logger = logging.getLogger("cds_sip2.sip2.server")


class _Connection:
    def __init__(self, reader, writer, peer):
        self.reader = reader
        self.writer = writer
        self.session = Session(peer=peer)
        self.busy = False


class Sip2Server:
    def __init__(self, dispatcher, settings):
        self.dispatcher = dispatcher
        self.settings = settings
        self._server = None
        self._connections = {}
        self._draining = False

    @property
    def port(self):
        return self._server.sockets[0].getsockname()[1]

    def is_serving(self):
        return self._server is not None and self._server.is_serving() and not self._draining

    async def start(self):
        self._server = await asyncio.start_server(
            self._accept,
            self.settings.bind_host,
            self.settings.port,
            # The stream buffer limit must cover a whole frame plus CR (and LF).
            limit=self.settings.max_frame_bytes + 2,
        )
        logger.info(
            "SIP2 server listening",
            extra={"event": "server.start", "host": self.settings.bind_host, "port": self.port},
        )

    async def _accept(self, reader, writer):
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else ""
        if self._draining or len(self._connections) >= self.settings.max_connections:
            logger.warning(
                "connection rejected",
                extra={
                    "event": "connection.rejected",
                    "peer": peer,
                    "reason": "draining" if self._draining else "max_connections",
                },
            )
            writer.close()
            return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        task = asyncio.current_task()
        conn = _Connection(reader, writer, peer)
        self._connections[task] = conn
        logger.info(
            "connection opened",
            extra={"event": "connection.open", "peer": peer, "open": len(self._connections)},
        )
        try:
            await self._serve(conn)
        except OSError as exc:
            logger.info(
                "connection error",
                extra={"event": "connection.error", "peer": peer, "error": repr(exc)},
            )
        finally:
            del self._connections[task]
            writer.close()
            logger.info(
                "connection closed",
                extra={
                    "event": "connection.close",
                    "peer": peer,
                    "terminal": conn.session.terminal,
                },
            )

    async def _serve(self, conn):
        settings = self.settings
        while not self._draining:
            try:
                async with asyncio.timeout(settings.idle_timeout):
                    frame = await read_frame(conn.reader, settings.max_frame_bytes)
            except TimeoutError:
                logger.info(
                    "idle timeout", extra={"event": "connection.idle", "peer": conn.session.peer}
                )
                return
            except FrameTooLarge:
                logger.warning(
                    "frame too large, closing",
                    extra={
                        "event": "sip2.oversize",
                        "peer": conn.session.peer,
                        "limit": settings.max_frame_bytes,
                    },
                )
                return
            except ConnectionClosed as exc:
                if exc.partial:
                    logger.warning(
                        "connection closed mid-frame",
                        extra={
                            "event": "sip2.truncated",
                            "peer": conn.session.peer,
                            "length": len(exc.partial),
                        },
                    )
                return
            if not frame:
                continue

            conn.busy = True
            try:
                outcome = await self.dispatcher.dispatch(frame, conn.session)
                if outcome.reply:
                    conn.writer.write(outcome.reply)
                    async with asyncio.timeout(settings.idle_timeout):
                        await conn.writer.drain()
            finally:
                conn.busy = False
            if outcome.close:
                return

    async def shutdown(self):
        """Stop accepting, then drain connections within ``shutdown_grace``."""
        self._draining = True
        if self._server is None:
            return
        self._server.close()
        for task, conn in list(self._connections.items()):
            if not conn.busy:
                task.cancel()
        pending = set(self._connections)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.settings.shutdown_grace)
        for task in pending:
            conn = self._connections.get(task)
            logger.error(
                "cancelling in-flight request at shutdown",
                extra={
                    "event": "server.shutdown",
                    "terminal": conn.session.terminal if conn else None,
                },
            )
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._server.wait_closed()
        logger.info("SIP2 server stopped", extra={"event": "server.stop"})
