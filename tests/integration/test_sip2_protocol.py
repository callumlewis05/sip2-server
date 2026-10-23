"""End-to-end SIP2 protocol test.

Spawns the *real* server (``invenio selfcheck start`` through the installed
``invenio_app`` CLI) as a subprocess, pointed at a fake ILS running in this
process and at a real Redis, then drives it with ``tests/sip2_client.py``
over TCP.  This is the only place where framing, AY/AZ checksums, the 98 BX
capability string and the handler wiring in ``SIP2_REMOTE_ACTION_HANDLERS``
are exercised together.

Requirements: ``pip install -e .`` (entry points!) and a Redis reachable at
``SIP2_TEST_REDIS_URL``.  Skipped otherwise.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

from tests import fake_ils
from tests import sip2_client as sip2

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("SIP2_TEST_REDIS_URL")
if not REDIS_URL:
    pytest.skip("SIP2_TEST_REDIS_URL not set", allow_module_level=True)

TERMINAL, PASSWORD = "kiosk1", "secret"
PATRON, PIN, ITEM = "P001", "1234", "IT001"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port, proc, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
    pytest.fail(f"SIP2 server did not start on port {port} (exit={proc.poll()}):\n{out}")


@pytest.fixture(scope="module")
def ils_server():
    with fake_ils.ThreadedServer() as server:
        yield server


@pytest.fixture(scope="module")
def sip2_server(ils_server, tmp_path_factory):
    port = _free_port()
    tmp = tmp_path_factory.mktemp("sip2")
    env = {
        **os.environ,
        "CDS_SIP2_ILS_URL": ils_server.base_url,
        "CDS_SIP2_ILS_TOKEN": fake_ils.TOKEN,
        "SIP2_DATASTORE_REDIS_URL": REDIS_URL,
        "SIP2_DATASTORE_REDIS_PREFIX": f"cds_sip2_test_{port}",
        "CDS_SIP2_LOG_FILE": str(tmp / "sip2.log"),
        "INVENIO_INSTANCE_PATH": str(tmp / "instance"),
        "SECRET_KEY": "integration-test",
        "SIP2_LOGGING_CONSOLE_LEVEL": "INFO",
    }
    # Use the CLI object directly instead of the ``invenio`` console script so
    # the test does not depend on the script being on PATH.
    cmd = [
        sys.executable, "-c", "import sys; from invenio_app.cli import cli; sys.exit(cli())",
        "selfcheck", "start", "integration-test",
        "-h", "127.0.0.1", "-p", str(port), "-r", "ils",
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for_port(port, proc)
        yield {"port": port, "proc": proc}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


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
    # Before a 93 the server may answer with an error frame or drop the
    # connection; either is fine, a 64 carrying patron data is not.
    try:
        response = conn.send(sip2.patron_information(PATRON, "CERN"))
    except (ConnectionError, OSError):
        return
    assert not (response.code == "64" and response.get("AE"))


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
    assert status.get("BX")  # supported messages


def test_full_selfcheck_session(conn):
    _login(conn)
    status = conn.send(sip2.sc_status())
    institution = status.get("AO", "")

    # 63 -> 64: patron with one existing loan (IT002) and a correct PIN.
    patron = conn.send(sip2.patron_information(PATRON, institution, pin=PIN))
    assert patron.code == "64", patron
    assert patron.get("AA") == PATRON
    assert patron.get("AE") == "Ada Lovelace"
    assert patron.get("BL") == "Y"
    assert patron.get("CQ") == "Y"
    assert int(patron.fixed["charged_items_count"]) == 1

    # 17 -> 18: available item.
    item = conn.send(sip2.item_information(ITEM, institution))
    assert item.code == "18", item
    assert item.fixed["circulation_status"] == "03"  # available
    assert item.get("AB") == ITEM
    assert item.get("AJ", "").startswith("Structure and Interpretation")

    # 11 -> 12: checkout succeeds, due date present.
    checkout = conn.send(sip2.checkout(PATRON, ITEM, institution, pin=PIN))
    assert checkout.code == "12", checkout
    assert checkout.ok, checkout
    assert checkout.fixed["desensitize"] == "Y"
    assert checkout.get("AH") and len(checkout.get("AH")) == 18
    assert checkout.get("AJ", "").startswith("Structure and Interpretation")

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


def test_blocked_patron_cannot_checkout(conn):
    _login(conn)
    checkout = conn.send(sip2.checkout("P002", ITEM, "CERN"))
    assert checkout.code == "12"
    assert not checkout.ok
    assert "blocked" in checkout.get("AF", "").lower()


def test_request_resend_repeats_last_response(conn):
    _login(conn)
    first = conn.send(sip2.sc_status())
    resent = conn.send(sip2.request_resend())
    assert resent.code == "98"
    assert resent.fixed["protocol_version"] == first.fixed["protocol_version"]


def test_every_response_carries_a_valid_checksum(conn):
    responses = [
        _login(conn),
        conn.send(sip2.sc_status()),
        conn.send(sip2.item_information(ITEM, "CERN")),
        conn.send(sip2.end_patron_session(PATRON, "CERN")),
    ]
    assert all(r.checksum_ok is True for r in responses), [str(r) for r in responses]
