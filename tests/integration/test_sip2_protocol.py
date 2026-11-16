"""End-to-end SIP2 protocol test.

Spawns the *real* server (``python -m cds_sip2``) as a subprocess, pointed at
a fake ILS running in this process, then drives it with
``tests/sip2_client.py`` over TCP. This is where framing, AY/AZ checksums,
the 98 BX capability string, the handler wiring, health endpoints and
SIGTERM handling are exercised together. No external services are needed.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests import fake_ils
from tests import sip2_client as sip2

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TERMINAL, PASSWORD = "kiosk1", "secret"
PATRON, PIN, ITEM, TITLE = "P001", "1234", "CM-B000456412", "Écoute"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port, proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    proc.kill()
    out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    pytest.fail(f"SIP2 server did not start on port {port} (exit={proc.poll()}):\n{out}")


@pytest.fixture(scope="module")
def ils_server():
    with fake_ils.ThreadedServer() as server:
        yield server


@pytest.fixture(scope="module")
def sip2_server(ils_server, tmp_path_factory):
    port, health_port = _free_port(), _free_port()
    log_path = tmp_path_factory.mktemp("sip2") / "sip2.log"
    env = {
        **os.environ,
        "CDS_SIP2_ILS_URL": ils_server.base_url,
        "CDS_SIP2_ILS_TOKEN": fake_ils.TOKEN,
        "CDS_SIP2_PORT": str(port),
        "CDS_SIP2_HEALTH_HOST": "127.0.0.1",
        "CDS_SIP2_HEALTH_PORT": str(health_port),
        "CDS_SIP2_LOG_LEVEL": "DEBUG",
        "CDS_SIP2_LOG_HASH_KEY": "integration-test",
    }
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cds_sip2"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_port(port, proc)
            yield {"port": port, "health_port": health_port, "proc": proc, "log": log_path}
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def conn(sip2_server):
    with sip2.Sip2Connection("127.0.0.1", sip2_server["port"], timeout=15, verbose=True) as c:
        yield c


def _login(conn):
    response = conn.send(sip2.login(TERMINAL, PASSWORD, "CERN"))
    assert response.code == "94", response
    assert response.ok, response
    return response


def test_login_refused_for_bad_password(conn):
    response = conn.send(sip2.login(TERMINAL, "wrong", "CERN"))
    assert response.code == "94"
    assert not response.ok


def test_unauthenticated_requests_do_not_get_patron_data(conn):
    response = conn.send(sip2.patron_information(PATRON, "CERN"))
    assert response.code == "64"
    assert response.get("BL") == "N"
    assert not response.get("AE")


def test_acs_status_advertises_configured_capabilities(conn):
    _login(conn)
    status = conn.send(sip2.sc_status())
    assert status.code == "98", status
    assert status.checksum_ok is True
    assert status.fixed["online_status"] == "Y"
    assert status.fixed["checkin_ok"] == "Y"
    assert status.fixed["checkout_ok"] == "Y"
    assert status.fixed["acs_renewal_policy"] == "Y"
    assert status.fixed["status_update_ok"] == "N"
    assert status.fixed["offline_ok"] == "N"
    assert status.fixed["protocol_version"] == "2.00"
    assert status.get("AO") == "CERN"
    assert status.get("AM") == "CERN Library"
    assert status.get("BX") == "YYYNYYYYYNYNYNYN"


def test_full_selfcheck_session(conn):
    _login(conn)
    status = conn.send(sip2.sc_status())
    institution = status.get("AO", "")

    # 63 -> 64: patron with two existing loans and a correct PIN.
    patron = conn.send(sip2.patron_information(PATRON, institution, pin=PIN))
    assert patron.code == "64", patron
    assert patron.get("AA") == PATRON
    assert patron.get("AE") == "Ada Lovelace"
    assert patron.get("BL") == "Y"
    assert patron.get("CQ") == "Y"
    assert int(patron.fixed["charged_items_count"]) == 2

    # 17 -> 18: available item.
    item = conn.send(sip2.item_information(ITEM, institution))
    assert item.code == "18", item
    assert item.fixed["circulation_status"] == "03"  # available
    assert item.get("AB") == ITEM
    assert item.get("AJ") == TITLE

    # 11 -> 12: checkout succeeds, due date present.
    checkout = conn.send(sip2.checkout(PATRON, ITEM, institution, pin=PIN))
    assert checkout.code == "12", checkout
    assert checkout.ok, checkout
    assert checkout.fixed["desensitize"] == "Y"
    assert checkout.get("AH") and len(checkout.get("AH")) == 18
    assert checkout.get("AJ") == TITLE

    # Second checkout of the same item is refused but still a well-formed 12.
    again = conn.send(sip2.checkout(PATRON, ITEM, institution, pin=PIN))
    assert again.code == "12", again
    assert not again.ok
    assert again.get("AF")  # screen message from the ILS

    # 17 -> 18 now reports the item as charged with a due date.
    item = conn.send(sip2.item_information(ITEM, institution))
    assert item.fixed["circulation_status"] == "04"
    assert item.get("AH")

    # 29 -> 30: renew.
    renew = conn.send(sip2.renew(PATRON, ITEM, institution, pin=PIN))
    assert renew.code == "30", renew
    assert renew.ok, renew

    # 09 -> 10: check the item back in.
    checkin = conn.send(sip2.checkin(ITEM, institution))
    assert checkin.code == "10", checkin
    assert checkin.ok, checkin
    assert checkin.get("AB") == ITEM
    assert checkin.fixed["resensitize"] == "Y"

    # Checking in an item that is not on loan is refused, not an error.
    again = conn.send(sip2.checkin(ITEM, institution))
    assert again.code == "10"
    assert not again.ok

    # 35 -> 36.
    end = conn.send(sip2.end_patron_session(PATRON, institution))
    assert end.code == "36", end
    assert end.fixed["end_session"] == "Y"


def test_unknown_patron_and_item(conn):
    _login(conn)
    patron = conn.send(sip2.patron_information("NOBODY", "CERN"))
    assert patron.code == "64"
    assert patron.get("BL") == "N"
    assert patron.fixed["patron_status"][0] == "Y"  # charge privileges denied

    item = conn.send(sip2.item_information("NOTHING", "CERN"))
    assert item.code == "18"
    assert item.fixed["circulation_status"] == "01"  # other

    checkout = conn.send(sip2.checkout(PATRON, "NOTHING", "CERN"))
    assert checkout.code == "12" and not checkout.ok
    assert checkout.get("AF") == "Item not found."


def test_blocked_patron_cannot_checkout(conn):
    _login(conn)
    checkout = conn.send(sip2.checkout("P002", ITEM, "CERN"))
    assert checkout.code == "12"
    assert not checkout.ok
    assert "blocked" in checkout.get("AF", "").lower()


def test_unsupported_message_gets_a_reply(conn):
    _login(conn)
    hold = conn.send(f"15+{sip2.now()}AOCERN|AA{PATRON}|AB{ITEM}|")
    assert hold.code == "16"
    assert hold.fixed == {}  # the reference client has no layout for 16
    assert conn.send(sip2.sc_status()).code == "98"  # connection still usable


def test_request_resend_repeats_last_response(conn):
    _login(conn)
    first = conn.send(sip2.sc_status())
    resent = conn.send(sip2.request_resend())
    assert resent.raw == first.raw


def test_bad_checksum_gets_96(sip2_server):
    with socket.create_connection(("127.0.0.1", sip2_server["port"]), timeout=5) as sock:
        good = sip2.frame(sip2.sc_status(), 0)
        sock.sendall((good[:-5] + "0000\r").encode())
        assert sock.recv(64) == b"96\r"


def test_resend_after_reconnect_is_idempotent(sip2_server):
    """Same frame on a new connection -> same transaction_id -> ILS replays the result."""
    body = f"11YN{sip2.now()}{sip2.BLANK_DATE}AOCERN|AA{PATRON}|AB{ITEM}|AC|"
    replies = []
    for _ in range(2):
        with sip2.Sip2Connection("127.0.0.1", sip2_server["port"], timeout=15) as c:
            _login(c)
            c.sequence = 5
            replies.append(c.send(body))
    assert replies[0].ok
    assert replies[1].ok, "the resend was treated as a new checkout"
    assert replies[1].get("AH") == replies[0].get("AH")

    with sip2.Sip2Connection("127.0.0.1", sip2_server["port"], timeout=15) as c:
        _login(c)
        assert c.send(sip2.checkin(ITEM, "CERN")).ok


def test_every_response_carries_a_valid_checksum(conn):
    responses = [
        _login(conn),
        conn.send(sip2.sc_status()),
        conn.send(sip2.item_information(ITEM, "CERN")),
        conn.send(sip2.end_patron_session(PATRON, "CERN")),
    ]
    assert all(r.checksum_ok is True for r in responses), [str(r) for r in responses]


def _health(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_health_endpoints(sip2_server):
    assert _health(sip2_server["health_port"], "/livez") == (200, {"sip2": True})
    assert _health(sip2_server["health_port"], "/readyz") == (200, {"sip2": True, "ils": True})


def test_zz_logs_are_json_and_free_of_secrets_then_sigterm_exits_cleanly(sip2_server):
    """Defined last, so it runs last: it stops the module's server."""
    proc = sip2_server["proc"]
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0

    lines = sip2_server["log"].read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert any(r.get("event") == "server.stop" for r in records)
    text = "\n".join(lines)
    for secret in (f"CO{PASSWORD}", f"AD{PIN}", "Ada Lovelace", f"AA{PATRON}"):
        assert secret not in text, secret
