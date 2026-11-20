"""Unit tests for ``cds_sip2.sip2.handlers`` against the in-memory ``FakeILS``.

Assertions are on the :class:`~cds_sip2.sip2.codec.Response` inside the
:class:`~cds_sip2.sip2.mapper.Reply` the handler returns: fixed fields by
name, variable fields by tag.
"""

import logging
import re
from dataclasses import replace
from datetime import date

import pytest

from cds_sip2.domain import Caller, CheckinResult, ItemStatus, MediaType
from cds_sip2.errors import (
    ILSAmbiguous,
    ILSConfigError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)
from cds_sip2.sip2 import enums, mapper
from cds_sip2.sip2.session import Session
from tests.conftest import ITEM, request, run

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


def reply(handlers, body, session, transaction_id="tx"):
    req = request(body)
    caller = session.caller(transaction_id) if session.authenticated else None
    return run(handlers.routes[req.code](mapper.parse(req), session, caller))


def call(handlers, body, session, transaction_id="tx"):
    return reply(handlers, body, session, transaction_id).response


# --------------------------------------------------------------------- login


def test_login_ok_uses_configured_terminal(handlers, fake_ils):
    session = Session(peer="10.0.0.5:1")

    response = call(handlers, "9300CNkiosk1|COsecret|CPloc|", session)

    assert response.code == "94" and response.fixed["ok"] == "1"
    assert session.authenticated
    assert session.terminal == "kiosk1"
    assert session.user_pid == "42"
    assert session.institution_id == "CERN"
    assert session.library_name == "CERN Library"
    assert session.language == "en"
    assert fake_ils.calls == []  # login never reaches the ILS


@pytest.mark.parametrize(
    "body", ["9300CNkiosk1|COwrong|CP|", "9300CNnobody|COsecret|CP|", "9300CN|CO|CP|"]
)
def test_login_refused_counts_attempts(handlers, body):
    session = Session()

    response = call(handlers, body, session)

    assert response.fixed["ok"] == "0"
    assert not session.authenticated
    assert session.failed_logins == 1


def test_failed_relogin_logs_the_session_out(handlers, session):
    call(handlers, "9300CNkiosk1|COwrong|CP|", session)
    assert not session.authenticated


# -------------------------------------------------------------------- status


def test_sc_status_capabilities_derive_from_routes(handlers, session):
    response = call(handlers, "9900402.00", session)

    assert response.code == "98"
    assert response.fixed["online_status"] == "Y"
    assert response.fixed["checkin_ok"] == "Y"
    assert response.fixed["checkout_ok"] == "Y"
    assert response.fixed["acs_renewal_policy"] == "N"  # 29 is not routed
    assert response.fixed["status_update_ok"] == "N"
    assert response.fixed["offline_ok"] == "N"
    assert response.fixed["timeout_period"] == "150"  # 15 s, in tenths
    assert response.fixed["retries_allowed"] == "003"
    assert response.fixed["protocol_version"] == "2.00"
    # 23 11 09 01 99 97 93 63 35 37 17 19 25 15 29 65
    assert tag(response, "BX") == "NYYNYYYNNNYYNNNN"
    assert tag(response, "AO") == "CERN"
    assert tag(response, "AM") == "CERN Library"


def test_sc_status_offline_and_before_login(handlers, fake_ils):
    fake_ils.answers["ping"] = False

    response = call(handlers, "9900402.00", Session())

    assert response.fixed["online_status"] == "N"
    assert tag(response, "AO") == "CERN"  # config fallback before login
    assert "unavailable" in tag(response, "AF").lower()


# --------------------------------------------------------------------- items


def test_item_information_available(handlers, session):
    response = call(handlers, f"17{NOW}AOCERN|AB IT001 |", session)

    assert response.code == "18"
    assert tag(response, "AB") == "IT001"
    assert tag(response, "AJ") == "SICP"
    assert response.fixed["circulation_status"] == "03"
    assert response.fixed["security_marker"] == "00"
    assert tag(response, "AQ") == "Main reading room"
    assert tag(response, "AP") == "Main reading room"  # falls back to location
    assert tag(response, "CF") == "2"
    assert tag(response, "CK") == "001"  # configured default: no media type in the record
    assert tag(response, "AH") is None


def test_item_information_on_loan_has_due_date(handlers, fake_ils, session):
    fake_ils.answers["get_item"] = replace(
        ITEM, status=ItemStatus.ON_LOAN, due_date=date(2026, 11, 4)
    )

    response = call(handlers, f"17{NOW}AOCERN|ABIT002|", session)

    assert response.fixed["circulation_status"] == "04"
    assert SIP2_DATE.match(tag(response, "AH"))
    assert tag(response, "AH").startswith("20261104")


def test_media_type_and_security_marker_come_from_item_and_settings(
    handlers, fake_ils, settings, session
):
    fake_ils.answers["get_item"] = replace(ITEM, media_type=MediaType.VIDEO)
    handlers.settings = replace(settings, security_marker="02", default_media_type="000")

    response = call(handlers, f"17{NOW}AOCERN|ABIT001|", session)

    assert tag(response, "CK") == "005"
    assert response.fixed["security_marker"] == "02"


@pytest.mark.parametrize("answer", [None, ILSUnavailable("down")])
def test_item_information_unknown_item_or_ils_down(handlers, fake_ils, session, answer):
    fake_ils.answers["get_item"] = answer

    response = call(handlers, f"17{NOW}AOCERN|ABNOPE|", session)

    assert response.fixed["circulation_status"] == "01"
    assert tag(response, "AB") == "NOPE"
    assert tag(response, "AJ") == ""
    assert tag(response, "AF")


def test_domain_vocabularies_are_fully_mapped():
    assert set(enums.CIRCULATION_STATUS) == set(ItemStatus)
    assert set(enums.MEDIA_TYPE) == set(MediaType)


# -------------------------------------------------------------------- writes

CHECKOUT = f"11YN{NOW}{BLANK}AOCERN|AAP001|ABIT001|AC|AD1234|"
CHECKIN = f"09N{NOW}{NOW}AP|AOCERN|ABIT001|AC|"
STATUS_UPDATE = f"19{NOW}AOCERN|ABIT001|AC|CHseen:shelf-3|"
CALLER = Caller(terminal="kiosk1", user_pid="42", transaction_id="abc")


def test_checkout_ok_passes_caller(handlers, fake_ils, session):
    result = reply(handlers, CHECKOUT, session, transaction_id="abc")
    response = result.response

    assert result.replayable
    assert response.code == "12"
    assert response.fixed["ok"] == "1"
    assert response.fixed["renewal_ok"] == "N"
    assert response.fixed["desensitize"] == "Y"
    assert tag(response, "AJ") == "SICP"
    assert tag(response, "AH").startswith("20261111")
    assert fake_ils.calls_to("checkout") == [
        {"item_barcode": "IT001", "patron_barcode": "P001", "caller": CALLER}
    ]


def test_checkin_ok(handlers, fake_ils, session):
    fake_ils.answers["checkin"] = CheckinResult(
        title="SICP", permanent_location="Shelf 3", alert=True, media_type=MediaType.CD
    )

    response = call(handlers, CHECKIN, session)

    assert response.code == "10"
    assert response.fixed["ok"] == "1"
    assert response.fixed["resensitize"] == "Y"
    assert response.fixed["alert"] == "Y"
    assert tag(response, "AQ") == "Shelf 3"
    assert tag(response, "CK") == "006"
    assert fake_ils.calls_to("checkin")[0]["caller"].user_pid == "42"


def test_item_status_update_passes_properties(handlers, fake_ils, session):
    response = call(handlers, STATUS_UPDATE, session, transaction_id="abc")

    assert response.code == "20"
    assert response.fixed["item_properties_ok"] == "1"
    assert tag(response, "AB") == "IT001"
    assert tag(response, "AJ") == "SICP"
    assert fake_ils.calls_to("update_item_status") == [
        {"item_barcode": "IT001", "properties": "seen:shelf-3", "caller": CALLER}
    ]


REJECTED = ILSRejected(
    409, {"code": "patron_blocked", "user_message": "Your account is blocked.", "title": "SICP"}
)


@pytest.mark.parametrize(
    "name, body, code, ok",
    [
        ("checkout", CHECKOUT, "12", "ok"),
        ("checkin", CHECKIN, "10", "ok"),
        ("update_item_status", STATUS_UPDATE, "20", "item_properties_ok"),
    ],
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
def test_write_failures(
    handlers, fake_ils, session, caplog, name, body, code, ok, exc, message, title, level, ambiguous
):
    fake_ils.answers[name] = exc

    with caplog.at_level(logging.DEBUG, logger="cds_sip2"):
        result = reply(handlers, body, session)

    response = result.response
    assert response.code == code
    assert response.fixed[ok] == "0"
    assert message.lower() in tag(response, "AF").lower()
    assert tag(response, "AJ", "") == title
    assert tag(response, "AB") == "IT001"
    assert result.replayable is not ambiguous
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
        (f"29NN{NOW}{BLANK}AOCERN|AAP001|ABIT001|AC|", "30"),
        (f"35{NOW}AOCERN|AAP001|", "36"),
        (f"01N{NOW}AOCERN|ALx|AAP001|AC|", "24"),
        (f"15+{NOW}AOCERN|AAP001|", "16"),
        (STATUS_UPDATE, "20"),
        (f"37{NOW}0100CHFBV1.00|AOCERN|AAP001|", "38"),
        (f"65{NOW}AOCERN|AAP001|", "66"),
        ("9900402.00", "98"),
    ],
)
def test_every_known_message_has_a_refusal(handlers, body, code):
    result = handlers.refuse(request(body), Session(), "not_logged_in")
    response = result.response
    assert response.code == code
    for name in ("ok", "item_properties_ok", "payment_accepted"):
        assert response.fixed.get(name, "0") in ("0", "N")
    if code == "98":  # a refused 99 reports the ACS as offline
        assert response.fixed["online_status"] == "N"
    elif code != "94":
        assert "not logged in" in tag(response, "AF").lower()


def test_patron_refusal_denies_privileges(handlers):
    response = handlers.refuse(request(f"23001{NOW}AOCERN|AAP001|AC|AD|"), Session(), "x").response
    assert response.fixed["patron_status"][:4] == "YY Y"  # charge, renewal, hold denied
    assert response.fixed["language"] == "001"  # configured language
    assert tag(response, "BL") == "N"
    assert tag(response, "AE") == ""
