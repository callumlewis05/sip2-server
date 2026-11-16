"""Unit tests for ``cds_sip2.sip2.handlers`` against the in-memory ``FakeILS``.

Assertions are on the :class:`~cds_sip2.sip2.codec.Response` the handler
returns: fixed fields by name, variable fields by tag.
"""

import logging
import re
from dataclasses import replace

import pytest

from cds_sip2.ils import (
    ILSAmbiguous,
    ILSConfigError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)
from cds_sip2.sip2 import enums
from cds_sip2.sip2.session import Session
from tests.conftest import ITEM, PATRON, request, run

SIP2_DATE = re.compile(r"^\d{8} {4}\d{6}$")
NOW = "20261102    120000"
BLANK = " " * 18


def tags(response):
    out = {}
    for tag, value in response.fields:
        out.setdefault(tag, []).append(value)
    return out


def tag(response, name, default=None):
    return tags(response).get(name, [default])[0]


def call(handlers, body, session, transaction_id="tx"):
    req = replace(request(body), transaction_id=transaction_id)
    return run(handlers.routes[req.code](req, session))


# --------------------------------------------------------------------- login


def test_login_ok_stores_terminal_and_user_pid(handlers, fake_ils):
    fake_ils.answers["login"] = {"ok": True, "user_pid": 42, "library_name": "Lib"}
    session = Session(peer="10.0.0.5:1")

    response = call(handlers, "9300CNkiosk1|COsecret|CPloc|", session)

    assert response.code == "94" and response.fixed["ok"] == "1"
    assert session.authenticated
    assert session.terminal == "kiosk1"
    assert session.user_pid == "42"
    assert session.institution_id == "CERN"  # config fallback
    assert session.library_name == "Lib"
    assert session.language == "en"  # config fallback
    assert fake_ils.calls_to("login") == [
        {"terminal": "kiosk1", "password": "secret", "ip": "10.0.0.5:1"}
    ]


def test_login_refused_counts_attempts(handlers, fake_ils):
    fake_ils.answers["login"] = None
    session = Session()

    response = call(handlers, "9300CNkiosk1|COwrong|CP|", session)

    assert response.fixed["ok"] == "0"
    assert not session.authenticated
    assert session.failed_logins == 1


@pytest.mark.parametrize("user_pid", [None, ""])
def test_login_refused_without_user_pid(handlers, fake_ils, user_pid, caplog):
    fake_ils.answers["login"] = {"ok": True, "user_pid": user_pid}
    session = Session()

    with caplog.at_level(logging.ERROR):
        response = call(handlers, "9300CNkiosk1|COsecret|CP|", session)

    assert response.fixed["ok"] == "0"
    assert not session.authenticated
    assert "no user_pid" in caplog.text


def test_failed_relogin_logs_the_session_out(handlers, fake_ils, session):
    fake_ils.answers["login"] = None
    call(handlers, "9300CNkiosk1|COwrong|CP|", session)
    assert not session.authenticated


def test_login_with_ils_down_is_refused(handlers, fake_ils):
    fake_ils.answers["login"] = ILSUnavailable("down")
    session = Session()
    assert call(handlers, "9300CNkiosk1|COsecret|CP|", session).fixed["ok"] == "0"
    assert session.failed_logins == 0  # an outage is not a wrong password


# -------------------------------------------------------------------- status


def test_sc_status_capabilities_derive_from_routes(handlers, session):
    response = call(handlers, "9900402.00", session)

    assert response.code == "98"
    assert response.fixed["online_status"] == "Y"
    assert response.fixed["checkin_ok"] == "Y"
    assert response.fixed["checkout_ok"] == "Y"
    assert response.fixed["acs_renewal_policy"] == "Y"
    assert response.fixed["status_update_ok"] == "N"
    assert response.fixed["offline_ok"] == "N"
    assert response.fixed["timeout_period"] == "015"
    assert response.fixed["retries_allowed"] == "003"
    assert response.fixed["protocol_version"] == "2.00"
    # 23 11 09 01 99 97 93 63 35 37 17 19 25 15 29 65
    assert tag(response, "BX") == "YYYNYYYYYNYNYNYN"
    assert tag(response, "AO") == "CERN"
    assert tag(response, "AM") == "CERN Library"


def test_sc_status_offline_and_before_login(handlers, fake_ils):
    fake_ils.answers["ping"] = False

    response = call(handlers, "9900402.00", Session())

    assert response.fixed["online_status"] == "N"
    assert tag(response, "AO") == "CERN"  # config fallback before login
    assert "unavailable" in tag(response, "AF").lower()


# ------------------------------------------------------------------- patrons


def test_patron_information_known_patron(handlers, fake_ils, session):
    response = call(handlers, f"63001{NOW}  Y       AOCERN|AA P001 |", session)

    assert response.code == "64"
    assert fake_ils.calls_to("get_patron") == [{"barcode": "P001"}]
    assert tag(response, "AA") == "P001"
    assert tag(response, "AE") == "Ada Lovelace"
    assert tag(response, "BL") == "Y"
    assert tag(response, "CQ") is None  # no AD sent
    assert tag(response, "BE") == "ada@example.org"
    assert tag(response, "BV") is None  # no fines
    assert response.fixed["patron_status"] == " " * 14
    assert response.fixed["charged_items_count"] == "0002"
    assert response.fixed["overdue_items_count"] == "0001"
    assert response.fixed["hold_items_count"] == "0001"
    assert response.fixed["fine_items_count"] == "0000"
    assert tags(response)["AU"] == ["IT002", "IT005"]  # summary asked for charged items
    assert "AS" not in tags(response)


def test_patron_information_summary_range(handlers, session):
    response = call(handlers, f"63001{NOW}  Y       AOCERN|AAP001|BP2|BQ2|", session)
    assert tags(response)["AU"] == ["IT005"]


def test_patron_information_with_fines_and_flags(handlers, fake_ils, session):
    fake_ils.answers["get_patron"] = {
        **PATRON,
        "fines_total": "12.50",
        "status_flags": ["charge_privileges_denied", "excessive_outstanding_fines"],
    }

    response = call(handlers, f"63001{NOW}{' ' * 10}AOCERN|AAP002|", session)

    assert tag(response, "BV") == "12.50"
    assert tag(response, "BH") == "CHF"
    flags = response.fixed["patron_status"]
    assert flags[0] == "Y"  # charge privileges denied is position 1
    assert flags[10] == "Y"  # excessive outstanding fines is position 11


def test_patron_information_checks_pin_when_sent(handlers, fake_ils, session):
    fake_ils.answers["verify_patron_pin"] = False

    response = call(handlers, f"63001{NOW}{' ' * 10}AOCERN|AAP001|AD|", session)

    assert tag(response, "CQ") == "N"
    # The handler must not short-circuit: the empty PIN reaches the ILS.
    assert fake_ils.calls_to("verify_patron_pin") == [{"barcode": "P001", "pin": ""}]


@pytest.mark.parametrize(
    "answer, message",
    [
        (None, "not recognised"),
        (ILSUnavailable("down"), "unavailable"),
    ],
)
def test_patron_information_unknown_or_ils_down(handlers, fake_ils, session, answer, message):
    fake_ils.answers["get_patron"] = answer

    response = call(handlers, f"63001{NOW}{' ' * 10}AOCERN|AANOPE|AD1234|", session)

    assert tag(response, "BL") == "N"
    assert tag(response, "AE") == ""
    assert response.fixed["charged_items_count"] == "0000"
    assert response.fixed["patron_status"] == enums.patron_status(enums.DENIED_FLAGS)[0]
    assert response.fixed["patron_status"][:4] == "YY Y"  # charge, renewal, hold denied
    assert message in tag(response, "AF").lower()
    assert not fake_ils.calls_to("verify_patron_pin")


def test_unknown_status_flag_is_logged_and_ignored(handlers, fake_ils, session, caplog):
    fake_ils.answers["get_patron"] = {**PATRON, "status_flags": ["not_a_real_flag"]}

    with caplog.at_level(logging.WARNING):
        response = call(handlers, f"63001{NOW}{' ' * 10}AOCERN|AAP001|", session)

    assert response.fixed["patron_status"] == " " * 14
    assert "unknown patron status" in caplog.text


@pytest.mark.parametrize(
    "body, code",
    [
        (f"23001{NOW}AOCERN|AAP001|AC|AD1234|", "24"),
        (f"25{NOW}AOCERN|AAP001|AC|AD1234|", "26"),
    ],
)
def test_patron_status_and_enable(handlers, session, body, code):
    response = call(handlers, body, session)

    assert response.code == code
    assert tag(response, "AE") == "Ada Lovelace"
    assert tag(response, "AA") == "P001"
    assert tag(response, "BL") == "Y"
    assert tag(response, "CQ") == "Y"


def test_patron_status_unknown_patron(handlers, fake_ils, session):
    fake_ils.answers["get_patron"] = None

    response = call(handlers, f"23001{NOW}AOCERN|AANOPE|AC|AD|", session)

    assert tag(response, "BL") == "N"
    assert tag(response, "AE") == ""
    assert response.fixed["patron_status"][0] == "Y"


def test_language_falls_back_to_configuration(handlers):
    session = Session()
    session.login("kiosk1", "42", "CERN", "Lib", None)
    response = call(handlers, f"23000{NOW}AOCERN|AAP001|AC|AD|", session)
    assert response.fixed["language"] == "001"


# --------------------------------------------------------------------- items


def test_item_information_available(handlers, session):
    response = call(handlers, f"17{NOW}AOCERN|ABIT001|", session)

    assert response.code == "18"
    assert tag(response, "AB") == "IT001"
    assert tag(response, "AJ") == "SICP"
    assert response.fixed["circulation_status"] == "03"
    assert tag(response, "AQ") == "Main reading room"
    assert tag(response, "AP") == "Main reading room"  # falls back to location
    assert tag(response, "CF") == "2"
    assert tag(response, "AH") is None


def test_item_information_on_loan_has_due_date(handlers, fake_ils, session):
    fake_ils.answers["get_item"] = {**ITEM, "status": "on_loan", "due_date": "2026-11-04"}

    response = call(handlers, f"17{NOW}AOCERN|ABIT002|", session)

    assert response.fixed["circulation_status"] == "04"
    assert SIP2_DATE.match(tag(response, "AH"))
    assert tag(response, "AH").startswith("20261104")


def test_unknown_item_status_maps_to_other_with_warning(handlers, fake_ils, session, caplog):
    fake_ils.answers["get_item"] = {**ITEM, "status": "teleported"}

    with caplog.at_level(logging.WARNING):
        response = call(handlers, f"17{NOW}AOCERN|ABIT001|", session)

    assert response.fixed["circulation_status"] == "01"
    assert "unknown ILS item status" in caplog.text


@pytest.mark.parametrize("answer", [None, ILSUnavailable("down")])
def test_item_information_unknown_item_or_ils_down(handlers, fake_ils, session, answer):
    fake_ils.answers["get_item"] = answer

    response = call(handlers, f"17{NOW}AOCERN|ABNOPE|", session)

    assert response.fixed["circulation_status"] == "01"
    assert tag(response, "AJ") == ""
    assert tag(response, "AF")


def test_item_status_vocabulary_is_fully_mapped():
    for status in (
        "available",
        "on_loan",
        "recalled",
        "on_hold_shelf",
        "in_transit",
        "in_process",
        "on_order",
        "lost",
        "missing",
        "other",
    ):
        assert status in enums.ITEM_STATUS


# --------------------------------------------------------------- circulation

CHECKOUT = f"11YN{NOW}{BLANK}AOCERN|AAP001|ABIT001|AC|AD1234|"
CHECKIN = f"09N{NOW}{NOW}AP|AOCERN|ABIT001|AC|"
RENEW = f"29NN{NOW}{BLANK}AOCERN|AAP001|ABIT001|AC|"


def test_checkout_ok_passes_session_identity_and_txid(handlers, fake_ils, session):
    response = call(handlers, CHECKOUT, session, transaction_id="abc")

    assert response.code == "12"
    assert response.fixed["ok"] == "1"
    assert response.fixed["renewal_ok"] == "N"
    assert response.fixed["desensitize"] == "Y"
    assert tag(response, "AJ") == "SICP"
    assert tag(response, "AH").startswith("20261111")
    assert not response.ambiguous
    assert fake_ils.calls_to("checkout") == [
        {
            "item_barcode": "IT001",
            "patron_barcode": "P001",
            "user_pid": "42",
            "terminal": "kiosk1",
            "transaction_id": "abc",
        }
    ]


def test_checkin_ok(handlers, fake_ils, session):
    fake_ils.answers["checkin"] = {"title": "SICP", "permanent_location": "Shelf 3", "alert": True}

    response = call(handlers, CHECKIN, session)

    assert response.code == "10"
    assert response.fixed["ok"] == "1"
    assert response.fixed["resensitize"] == "Y"
    assert response.fixed["alert"] == "Y"
    assert tag(response, "AQ") == "Shelf 3"
    assert fake_ils.calls_to("checkin")[0]["user_pid"] == "42"


def test_renew_ok(handlers, session):
    response = call(handlers, RENEW, session)

    assert response.code == "30"
    assert response.fixed["ok"] == "1"
    assert response.fixed["renewal_ok"] == "Y"
    assert tag(response, "AH").startswith("20261201")


REJECTED = ILSRejected(
    409, {"code": "patron_blocked", "user_message": "Your account is blocked.", "title": "SICP"}
)


@pytest.mark.parametrize(
    "name, body, code",
    [("checkout", CHECKOUT, "12"), ("checkin", CHECKIN, "10"), ("renew", RENEW, "30")],
)
@pytest.mark.parametrize(
    "exc, message, title, level, ambiguous",
    [
        (REJECTED, "Your account is blocked.", "SICP", logging.INFO, False),
        (ILSNotFound("404"), "Item not found.", "", logging.INFO, False),
        (ILSUnavailable("down"), "unavailable", "", logging.ERROR, False),
        (ILSConfigError("401"), "unavailable", "", logging.ERROR, False),
        (ILSAmbiguous("read timeout", "tx"), "could not confirm", "", logging.ERROR, True),
    ],
)
def test_circulation_failures(
    handlers, fake_ils, session, caplog, name, body, code, exc, message, title, level, ambiguous
):
    fake_ils.answers[name] = exc

    with caplog.at_level(logging.DEBUG, logger="cds_sip2"):
        response = call(handlers, body, session)

    assert response.code == code
    assert response.fixed["ok"] == "0"
    assert message.lower() in tag(response, "AF").lower()
    assert tag(response, "AJ") == title
    assert response.ambiguous is ambiguous
    assert len(fake_ils.calls_to(name)) == 1  # never retried
    record = [r for r in caplog.records if r.name == "cds_sip2.sip2.handlers"][-1]
    assert record.levelno == level
    if ambiguous:
        assert record.transaction_id == "tx"
    if isinstance(exc, ILSConfigError):
        assert record.alert is True


def test_patron_barcode_is_pseudonymised_in_logs(handlers, session, caplog):
    with caplog.at_level(logging.INFO):
        call(handlers, CHECKOUT, session)
    record = [r for r in caplog.records if getattr(r, "event", None) == "checkout"][-1]
    assert record.patron.startswith("p:") and "P001" not in record.patron
    assert record.item == "IT001"


def test_end_patron_session(handlers, session):
    response = call(handlers, f"35{NOW}AOCERN|AAP001|", session)
    assert response.code == "36"
    assert response.fixed["end_session"] == "Y"
    assert tag(response, "AA") == "P001"


# ----------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "body, code",
    [
        ("9300CNk|COp|CP|", "94"),
        (f"23001{NOW}AOCERN|AAP001|AC|AD|", "24"),
        (f"25{NOW}AOCERN|AAP001|", "26"),
        (f"63001{NOW}{' ' * 10}AOCERN|AAP001|", "64"),
        (f"17{NOW}AOCERN|ABIT001|", "18"),
        (CHECKOUT, "12"),
        (CHECKIN, "10"),
        (RENEW, "30"),
        (f"35{NOW}AOCERN|AAP001|", "36"),
        (f"01N{NOW}AOCERN|ALx|AAP001|AC|", "24"),
        (f"15+{NOW}AOCERN|AAP001|", "16"),
        (f"19{NOW}AOCERN|ABIT001|AC|", "20"),
        (f"37{NOW}0100CHFBV1.00|AOCERN|AAP001|", "38"),
        (f"65{NOW}AOCERN|AAP001|", "66"),
        ("9900402.00", "98"),
    ],
)
def test_every_known_message_has_a_refusal(handlers, body, code):
    response = handlers.refuse(request(body), Session(), "not_logged_in")
    assert response.code == code
