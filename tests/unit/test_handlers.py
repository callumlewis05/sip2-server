"""Unit tests for ``cds_sip2.handlers``.

Each test runs inside the ``app`` fixture's application context with the ILS
mocked by ``responses``.  Assertions use the same properties the invenio-sip2
actions read (``is_success``, ``charged_items_count``, ...), so a passing
suite means the actions will see what they expect.
"""

import re

import pytest
import requests
import responses
from invenio_sip2.errors import SelfcheckCirculationError
from invenio_sip2.models import (
    SelfcheckCheckin,
    SelfcheckCheckout,
    SelfcheckCirculationStatus,
    SelfcheckItemInformation,
    SelfcheckPatronInformation,
    SelfcheckRenew,
)

from cds_sip2 import handlers
from tests.conftest import url
import json

SIP2_DATE = re.compile(r"^\d{8} {4}\d{6}$")

PATRON = {
    "barcode": "P001", "name": "Ada Lovelace", "email": "ada@example.org",
    "status_flags": [], "loans": ["IT002", "IT005"], "overdue": ["IT005"],
    "holds": ["IT009"], "recalls": [], "unavailable_holds": [], "fines": [],
    "fines_total": "0.00", "currency": "CHF",
}
ITEM = {
    "barcode": "IT001", "title": "SICP", "status": "available",
    "location": "Main reading room", "current_location": None, "due_date": None,
    "hold_queue_length": 2, "owner": "CERN Library",
}


# --------------------------------------------------------------------- login


def test_login_ok_returns_session_dict(ils):
    ils.post(url("/login"), json={"ok": True, "user_pid": 42, "location_pid": "1",
                                  "library_name": "CERN Library"})

    session = handlers.selfcheck_login("kiosk1", "secret", terminal_ip="10.0.0.5")

    assert session["authenticated"] is True
    assert session["terminal"] == "kiosk1"
    assert session["transaction_user_id"] == "42"
    assert session["institution_id"] == "CERN"  # config fallback
    assert session["library_name"] == "CERN Library"
    assert session["library_language"] == "en"


def test_login_refused_returns_none(ils):
    ils.post(url("/login"), json={"ok": False})
    assert handlers.selfcheck_login("kiosk1", "wrong") is None


def test_login_with_ils_down_returns_none(ils):
    ils.post(url("/login"), body=requests.exceptions.ConnectTimeout())
    assert handlers.selfcheck_login("kiosk1", "secret") is None


def test_system_status_reports_online_flag(ils):
    ils.get(url("/ping"), json={"ok": True})
    assert handlers.system_status("kiosk1", institution_id="CERN")["online"] is True

    ils.replace(responses.GET, url("/ping"), status=503)
    assert handlers.system_status("kiosk1")["online"] is False


# ------------------------------------------------------------------- patrons


def test_patron_information_known_patron(ils):
    ils.get(url("/patrons/P001"), json=PATRON)

    info = handlers.patron_information(" P001 ", institution_id="CERN")

    assert isinstance(info, SelfcheckPatronInformation)
    assert info.patron_id == "P001"
    assert info.patron_name == "Ada Lovelace"
    assert info.charged_items_count == 2
    assert info.overdue_items_count == 1
    assert info.hold_items_count == 1
    assert info.fine_items_count == 0
    assert info["valid_patron"] == "Y"
    assert info["email"] == "ada@example.org"
    assert "fee_amount" not in info
    assert str(info["patron_status"]) == " " * 14  # no flags set


def test_patron_information_with_fines_and_flags(ils):
    ils.get(url("/patrons/P002"), json={
        **PATRON, "barcode": "P002", "fines_total": "12.50",
        "status_flags": ["charge_privileges_denied", "excessive_outstanding_fines"],
    })

    info = handlers.patron_information("P002")

    assert info["fee_amount"] == "12.50"
    assert info["currency_type"] == "CHF"
    flags = str(info["patron_status"])
    assert len(flags) == 14
    assert flags[0] == "Y"  # charge privileges denied is position 1
    assert flags[10] == "Y"  # excessive outstanding fines is position 11


def test_patron_information_unknown_patron(ils):
    ils.get(url("/patrons/NOPE"), status=404)

    info = handlers.patron_information("NOPE", institution_id="CERN")

    assert info["valid_patron"] == "N"
    assert info.charged_items_count == 0
    assert str(info["patron_status"])[0] == "Y"  # charge privileges denied
    assert info["screen_messages"]


def test_patron_information_ils_down(ils):
    ils.get(url("/patrons/P001"), status=500)

    info = handlers.patron_information("P001")

    assert info["valid_patron"] == "N"
    assert "unavailable" in info["screen_messages"][0].lower()


def test_validate_and_authorize_patron(ils):
    ils.get(url("/patrons/P001"), json=PATRON)
    ils.get(url("/patrons/NOPE"), status=404)
    ils.post(url("/patrons/P001/verify"), json={"ok": True})

    assert handlers.validate_patron("P001") is True
    assert handlers.validate_patron("NOPE") is False
    assert handlers.authorize_patron("P001", "1234") is True


def test_authorize_patron_defers_empty_pin_to_ils(ils):
    ils.post(url("/patrons/P001/verify"), json={"ok": False})

    assert handlers.authorize_patron("P001", "") is False
    # The handler must not short-circuit: the empty PIN reaches the ILS.
    assert len(ils.calls) == 1
    assert json.loads(ils.calls[0].request.body) == {"pin": ""}


def test_patron_status_and_enable(ils):
    ils.get(url("/patrons/P001"), json=PATRON)

    status = handlers.patron_status("P001", institution_id="CERN")
    assert status["patron_name"] == "Ada Lovelace"
    assert status["valid_patron"] == "Y"

    enabled = handlers.enable_patron("P001", institution_id="CERN")
    assert enabled["patron_name"] == "Ada Lovelace"
    assert enabled["patron_id"] == "P001"


def test_unknown_status_flag_is_ignored(ils):
    ils.get(url("/patrons/P001"), json={**PATRON, "status_flags": ["not_a_real_flag"]})
    info = handlers.patron_information("P001")
    assert str(info["patron_status"]) == " " * 14


# --------------------------------------------------------------------- items


def test_item_information_available(ils):
    ils.get(url("/items/IT001"), json=ITEM)

    info = handlers.item_information("IT001", terminal="kiosk1", language="en")

    assert isinstance(info, SelfcheckItemInformation)
    assert info["item_id"] == "IT001"
    assert info["title_id"] == "SICP"
    assert info["circulation_status"] == SelfcheckCirculationStatus.AVAILABLE
    assert info["permanent_location"] == "Main reading room"
    assert info["current_location"] == "Main reading room"
    assert info["hold_queue_length"] == "2"
    assert "due_date" not in info


def test_item_information_on_loan_has_due_date(ils):
    ils.get(url("/items/IT002"), json={**ITEM, "status": "on_loan", "due_date": "2026-11-04"})

    info = handlers.item_information("IT002")

    assert info["circulation_status"] == SelfcheckCirculationStatus.CHARGED
    assert SIP2_DATE.match(info["due_date"])
    assert info["due_date"].startswith("20261104")


def test_item_information_unknown_item(ils):
    ils.get(url("/items/NOPE"), status=404)

    info = handlers.item_information("NOPE")

    assert info["circulation_status"] == SelfcheckCirculationStatus.OTHER
    assert info["title_id"] == ""
    assert info["screen_messages"]


def test_item_status_vocabulary_is_fully_mapped():
    for status in ("available", "on_loan", "recalled", "on_hold_shelf", "in_transit",
                   "in_process", "on_order", "lost", "missing"):
        assert status in handlers.ITEM_STATUS_MAP


# --------------------------------------------------------------- circulation


def test_checkout_ok(ils):
    ils.post(url("/checkout"), json={"title": "SICP", "due_date": "2026-11-11",
                                     "is_renewal": False})

    result = handlers.checkout("42", "IT001", "P001", institution_id="CERN",
                               terminal="kiosk1", language="en")

    assert isinstance(result, SelfcheckCheckout)
    assert result.is_success
    assert result.is_renewal is False
    assert result["title_id"] == "SICP"
    assert SIP2_DATE.match(result.due_date)
    assert result.due_date.startswith("20261111")

    import json

    body = json.loads(ils.calls[0].request.body)
    assert body["user_pid"] == "42"
    assert body["terminal"] == "kiosk1"
    assert len(body["transaction_id"]) == 32


def test_checkout_rejected_raises_with_negative_response(ils):
    ils.post(url("/checkout"), status=409, json={
        "code": "patron_blocked", "message": "blocked",
        "user_message": "Your account is blocked.", "title": "SICP",
    })

    with pytest.raises(SelfcheckCirculationError) as info:
        handlers.checkout("42", "IT001", "P002", terminal="kiosk1")

    data = info.value.data
    assert isinstance(data, SelfcheckCheckout)
    assert not data.is_success
    assert data["title_id"] == "SICP"
    assert data["screen_messages"] == ["Your account is blocked."]


def test_checkout_with_ils_down_raises_generic_failure(ils):
    ils.post(url("/checkout"), body=requests.exceptions.ReadTimeout())

    with pytest.raises(SelfcheckCirculationError) as info:
        handlers.checkout("42", "IT001", "P001", terminal="kiosk1")

    assert not info.value.data.is_success
    assert "unavailable" in info.value.data["screen_messages"][0].lower()
    assert len(ils.calls) == 1  # no retry


def test_checkin_ok(ils):
    ils.post(url("/checkin"), json={"title": "SICP", "permanent_location": "Main reading room",
                                    "alert": True})

    result = handlers.checkin("42", "IT001", institution_id="CERN", terminal="kiosk1")

    assert isinstance(result, SelfcheckCheckin)
    assert result.is_success
    assert result["permanent_location"] == "Main reading room"
    assert result["title_id"] == "SICP"
    assert result["alert"] is True


def test_checkin_rejected(ils):
    ils.post(url("/checkin"), status=409, json={"code": "item_not_on_loan",
                                                "user_message": "Not on loan.", "title": "SICP"})

    with pytest.raises(SelfcheckCirculationError) as info:
        handlers.checkin("42", "IT001", terminal="kiosk1")

    assert not info.value.data.is_success
    assert info.value.data["screen_messages"] == ["Not on loan."]


def test_renew_ok_accepts_misspelled_institution_kwarg(ils):
    ils.post(url("/renew"), json={"title": "SICP", "due_date": "2026-12-01"})

    # invenio-sip2 passes ``intitution_id`` (sic); it must not break us.
    result = handlers.renew("42", "IT001", patron_id="P001", intitution_id="CERN",
                            terminal="kiosk1", language="en")

    assert isinstance(result, SelfcheckRenew)
    assert result.is_success
    assert result.is_renewal
    assert result["due_date"].startswith("20261201")


def test_renew_rejected(ils):
    ils.post(url("/renew"), status=409, json={"code": "not_borrower",
                                              "user_message": "Not yours.", "title": "SICP"})

    with pytest.raises(SelfcheckCirculationError) as info:
        handlers.renew("42", "IT001", patron_id="P009", terminal="kiosk1")

    assert not info.value.data.is_success
    assert info.value.data["screen_messages"] == ["Not yours."]
