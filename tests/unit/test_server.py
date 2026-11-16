"""The asyncio server, in-process on an ephemeral port, with the FakeILS behind it."""

import asyncio
import socket
from dataclasses import replace

import pytest

from cds_sip2.health import HealthServer, ILSProbe
from cds_sip2.sip2.dispatcher import Dispatcher
from cds_sip2.sip2.handlers import Handlers
from cds_sip2.sip2.server import Sip2Server
from tests import sip2_client
from tests.conftest import run

NOW = "20261102    120000"
CHECKOUT = f"11YN{NOW}{' ' * 18}AOCERN|AAP001|ABIT001|AC|"


def serving(fake_ils, settings, scenario, **overrides):
    """Start a server with ``overrides`` applied to settings and run ``scenario(server)``."""
    settings = replace(settings, **overrides)

    async def go():
        server = Sip2Server(Dispatcher(Handlers(fake_ils, settings), settings), settings)
        await server.start()
        try:
            return await scenario(server)
        finally:
            await server.shutdown()

    return run(go())


async def connect(server):
    return await asyncio.open_connection("127.0.0.1", server.port)


async def exchange(reader, writer, body, sequence=0):
    writer.write(sip2_client.frame(body, sequence).encode())
    await writer.drain()
    return sip2_client.parse((await asyncio.wait_for(reader.readuntil(b"\r"), 5)).decode())


async def closed(reader):
    return await asyncio.wait_for(reader.read(), 5) == b""


def test_session_over_tcp(fake_ils, settings):
    async def scenario(server):
        reader, writer = await connect(server)
        login = await exchange(reader, writer, "9300CNkiosk1|COsecret|CP|", 0)
        checkout = await exchange(reader, writer, CHECKOUT, 1)
        writer.close()
        return login, checkout

    login, checkout = serving(fake_ils, settings, scenario)
    assert login.ok and checkout.ok and checkout.sequence == 1


def test_crlf_terminated_frames_are_accepted(fake_ils, settings):
    async def scenario(server):
        reader, writer = await connect(server)
        writer.write(sip2_client.frame("9900402.00", 0).encode() + b"\n")
        writer.write(sip2_client.frame("9900402.00", 1).encode() + b"\n")
        first = await reader.readuntil(b"\r")
        second = await reader.readuntil(b"\r")
        writer.close()
        return sip2_client.parse(first.decode()), sip2_client.parse(second.decode())

    first, second = serving(fake_ils, settings, scenario)
    assert first.code == second.code == "98"
    assert second.sequence == 1


def test_oversize_frame_closes_connection(fake_ils, settings):
    async def scenario(server):
        reader, writer = await connect(server)
        writer.write(b"17" + b"x" * 500)
        await writer.drain()
        return await closed(reader)

    assert serving(fake_ils, settings, scenario, max_frame_bytes=256)


def test_garbage_closes_connection(fake_ils, settings):
    async def scenario(server):
        reader, writer = await connect(server)
        writer.write(b"\x16\x03\x01\x02\x00\r")
        return await closed(reader)

    assert serving(fake_ils, settings, scenario)


def test_idle_connection_is_closed(fake_ils, settings):
    async def scenario(server):
        reader, _ = await connect(server)
        return await closed(reader)

    assert serving(fake_ils, settings, scenario, idle_timeout=0.2)


def test_max_connections(fake_ils, settings):
    async def scenario(server):
        first = await connect(server)
        await exchange(*first, "9900402.00")  # make sure the first is registered
        reader, _ = await connect(server)
        rejected = await closed(reader)
        still_ok = await exchange(*first, "9900402.00", 1)
        return rejected, still_ok

    rejected, still_ok = serving(fake_ils, settings, scenario, max_connections=1)
    assert rejected and still_ok.code == "98"


def test_keepalive_is_enabled(fake_ils, settings):
    async def scenario(server):
        client = await connect(server)
        await exchange(*client, "9900402.00")
        (conn,) = server._connections.values()
        sock = conn.writer.get_extra_info("socket")
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)

    assert serving(fake_ils, settings, scenario)


def test_shutdown_lets_in_flight_request_finish(fake_ils, settings):
    async def scenario(server):
        gate = asyncio.Event()

        async def slow_checkout(**kwargs):
            await gate.wait()
            return {"title": "SICP", "due_date": "2026-11-11"}

        fake_ils.answers["checkout"] = slow_checkout
        busy = await connect(server)
        idle = await connect(server)
        await exchange(*busy, "9300CNkiosk1|COsecret|CP|")
        busy[1].write(sip2_client.frame(CHECKOUT, 1).encode())
        await asyncio.sleep(0.05)

        shutdown = asyncio.create_task(server.shutdown())
        await asyncio.sleep(0.05)
        idle_closed = await closed(idle[0])
        refused = not server.is_serving()
        gate.set()
        reply = sip2_client.parse((await busy[0].readuntil(b"\r")).decode())
        await shutdown
        return idle_closed, refused, reply

    idle_closed, refused, reply = serving(fake_ils, settings, scenario, shutdown_grace=5)
    assert idle_closed and refused
    assert reply.code == "12" and reply.ok


def test_shutdown_cancels_requests_beyond_grace(fake_ils, settings):
    async def scenario(server):
        async def hang(**kwargs):
            await asyncio.sleep(60)

        fake_ils.answers["checkout"] = hang
        reader, writer = await connect(server)
        await exchange(reader, writer, "9300CNkiosk1|COsecret|CP|")
        writer.write(sip2_client.frame(CHECKOUT, 1).encode())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(server.shutdown(), 5)
        return await closed(reader)

    assert serving(fake_ils, settings, scenario, shutdown_grace=0.1)


# ------------------------------------------------------------------- health


async def http_get(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    status = (await reader.read()).split(b" ", 2)[1]
    writer.close()
    return int(status)


@pytest.mark.parametrize("ils_up, ready", [(True, 200), (False, 503)])
def test_health_liveness_ignores_ils_readiness_does_not(fake_ils, settings, ils_up, ready):
    fake_ils.answers["ping"] = ils_up

    async def scenario(server):
        health = HealthServer(server, ILSProbe(fake_ils, ttl=10), settings)
        await health.start()
        try:
            return [await http_get(health.port, path) for path in ("/livez", "/readyz", "/nope")]
        finally:
            await health.close()

    assert serving(fake_ils, settings, scenario) == [200, ready, 404]


def test_health_reports_dead_listener(fake_ils, settings):
    async def scenario(server):
        health = HealthServer(server, ILSProbe(fake_ils, ttl=10), settings)
        await health.start()
        await server.shutdown()
        try:
            return await http_get(health.port, "/livez")
        finally:
            await health.close()

    assert serving(fake_ils, settings, scenario) == 503


def test_ils_probe_caches(fake_ils):
    now = [0.0]
    probe = ILSProbe(fake_ils, ttl=10, clock=lambda: now[0])

    async def scenario():
        await probe.ok()
        await probe.ok()
        now[0] = 11
        await probe.ok()

    run(scenario())
    assert len(fake_ils.calls_to("ping")) == 2
