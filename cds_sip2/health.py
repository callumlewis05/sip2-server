"""Liveness and readiness over plain HTTP, for OpenShift probes.

* ``GET /livez``  200 while the SIP2 listener is accepting. Wire this to the
  liveness probe: only a dead server should restart the pod.
* ``GET /readyz`` 200 when the listener is accepting *and* the ILS answers
  its ping (cached for ``health_ils_cache`` seconds, so probes do not hammer
  the ILS). An ILS outage makes the pod unready, never restarts it.

The listener is a minimal HTTP/1.0 responder: no framework, no keep-alive.
"""

import asyncio
import json
import logging
import time

logger = logging.getLogger("cds_sip2.health")


class ILSProbe:
    """Cached ``ils.ping()``; concurrent callers share one in-flight ping."""

    def __init__(self, ils, ttl, clock=time.monotonic):
        self.ils = ils
        self.ttl = ttl
        self._clock = clock
        self._checked_at = None
        self._ok = False
        self._lock = asyncio.Lock()

    async def ok(self):
        async with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= self.ttl:
                self._ok = await self.ils.ping()
                self._checked_at = self._clock()
            return self._ok


class HealthServer:
    def __init__(self, sip2_server, probe, settings):
        self.sip2_server = sip2_server
        self.probe = probe
        self.settings = settings
        self._server = None

    @property
    def port(self):
        return self._server.sockets[0].getsockname()[1]

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, self.settings.health_host, self.settings.health_port, limit=8192
        )
        logger.info(
            "health endpoint listening",
            extra={"event": "health.start", "host": self.settings.health_host, "port": self.port},
        )

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def status(self, path):
        serving = self.sip2_server.is_serving()
        if path == "/livez":
            return serving, {"sip2": serving}
        if path == "/readyz":
            ils = await self.probe.ok() if serving else False
            return serving and ils, {"sip2": serving, "ils": ils}
        return None, {"error": "not found"}

    async def _handle(self, reader, writer):
        try:
            async with asyncio.timeout(5):
                head = await reader.readuntil(b"\r\n\r\n")
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            path = parts[1].split("?", 1)[0] if len(parts) >= 2 and parts[0] == "GET" else ""
            ok, body = await self.status(path)
            status = (
                "404 Not Found" if ok is None else "200 OK" if ok else "503 Service Unavailable"
            )
            payload = json.dumps(body).encode()
            writer.write(
                f"HTTP/1.0 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        except TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError:
            pass
        finally:
            writer.close()
