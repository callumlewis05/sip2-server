"""Unit tests for ``cds_sip2.rest.client`` (ILS mocked with ``httpx.MockTransport``)."""

import logging
from datetime import date

import httpx
import pytest

from cds_sip2.domain import (
    Caller,
    CheckinResult,
    CheckoutResult,
    ILSBackend,
    Item,
    ItemStatus,
    MediaType,
)
from cds_sip2.errors import (
    ILSAmbiguous,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)
from cds_sip2.rest import ILSClient
from tests.conftest import ILS_URL, run

CALLER = Caller(terminal="kiosk1", user_pid="42", transaction_id="tx1")
CIRCULATION = {"caller": CALLER}


@pytest.fixture
def call(router):
    """``call("get_item", "IT1")`` -> runs the client method against ``router``."""

    def _call(method, *args, **kwargs):
        async def go():
            client: ILSBackend = ILSClient(
                ILS_URL + "/", "tok", 0.5, 0.5, transport=httpx.MockTransport(router)
            )
            try:
                return await getattr(client, method)(*args, **kwargs)
            finally:
                await client.aclose()

        return run(go())

    return _call


def checkout(call):
    return call("checkout", item_barcode="IT1", patron_barcode="P001", **CIRCULATION)


def test_sends_bearer_token_and_accept_json(call, router):
    router.get("/items/IT1", json={"barcode": "IT1", "status": "available"})

    item = call("get_item", "IT1")

    assert item.status is ItemStatus.AVAILABLE
    sent = router.calls[0]
    assert sent.headers["Authorization"] == "Bearer tok"
    assert sent.headers["Accept"] == "application/json"


def test_barcode_is_one_path_segment(call, router):
    router.get("/items/A%2FB%3F", json={"barcode": "A/B?"})
    assert call("get_item", "A/B?").barcode == "A/B?"


def test_lookups_return_none_on_404(call, router):
    router.get("/items/NOPE", status=404)

    assert call("get_item", "NOPE") is None


def test_404_on_transaction_raises_not_found(call, router):
    router.post("/checkout", status=404, json={"code": "item_not_found"})

    with pytest.raises(ILSNotFound):
        checkout(call)


def test_5xx_on_lookup_raises_unavailable(call, router):
    router.get("/items/IT1", status=503)

    with pytest.raises(ILSUnavailable):
        call("get_item", "IT1")


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_transport_errors_on_lookup_raise_unavailable(call, router, exc):
    router.get("/items/T", exc=exc)

    with pytest.raises(ILSUnavailable):
        call("get_item", "T")


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout, httpx.ConnectError])
def test_connect_failure_on_circulation_is_not_ambiguous(call, router, exc):
    """Nothing reached the ILS, so the kiosk may safely be told "unavailable"."""
    router.post("/checkout", exc=exc)

    with pytest.raises(ILSUnavailable):
        checkout(call)


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteTimeout]
)
def test_lost_answer_on_circulation_is_ambiguous(call, router, exc):
    router.post("/checkout", exc=exc)

    with pytest.raises(ILSAmbiguous) as info:
        checkout(call)
    assert info.value.transaction_id == "tx1"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_on_circulation_is_ambiguous(call, router, status):
    router.post("/items/IT1/status", status=status)

    with pytest.raises(ILSAmbiguous):
        call("update_item_status", item_barcode="IT1", properties="x", **CIRCULATION)


def test_ambiguous_is_not_unavailable():
    """Handlers must be able to tell the two apart with ``except`` order alone."""
    assert not issubclass(ILSAmbiguous, ILSUnavailable)


@pytest.mark.parametrize("status", [401, 403])
def test_service_token_rejection_raises_config_error(call, router, status):
    router.get("/ping", status=status)
    router.get("/items/IT1", status=status)
    router.post("/checkout", status=status)

    assert call("ping") is False  # ping never raises
    with pytest.raises(ILSConfigError):
        call("get_item", "IT1")
    with pytest.raises(ILSConfigError):
        checkout(call)


def test_4xx_with_body_raises_rejected_with_user_message(call, router):
    router.post(
        "/checkout",
        status=409,
        json={
            "code": "item_not_available",
            "message": "status is on_loan",
            "user_message": "This item is not available.",
            "title": "SICP",
        },
    )

    with pytest.raises(ILSRejected) as info:
        checkout(call)

    assert info.value.code == "item_not_available"
    assert info.value.user_message == "This item is not available."
    assert info.value.title == "SICP"
    assert info.value.status == 409


def test_4xx_without_json_body_still_rejected(call, router):
    router.post("/checkin", status=400, content=b"nope")

    with pytest.raises(ILSRejected) as info:
        call("checkin", item_barcode="IT1", **CIRCULATION)
    assert info.value.code == "rejected"


def test_ok_false_on_circulation_is_rejected(call, router):
    router.post("/checkin", json={"ok": False, "code": "nope", "user_message": "No."})

    with pytest.raises(ILSRejected) as info:
        call("checkin", item_barcode="IT1", **CIRCULATION)
    assert info.value.user_message == "No."


def test_posts_are_never_retried(call, router):
    router.post("/checkout", status=500)

    with pytest.raises(ILSAmbiguous):
        checkout(call)
    assert len(router.calls) == 1


def test_checkout_payload_shape(call, router):
    router.post("/checkout", json={"title": "SICP", "due_date": "2026-11-11"})

    checkout(call)

    assert router.body() == {
        "item_barcode": "IT1",
        "patron_barcode": "P001",
        "user_pid": "42",
        "terminal": "kiosk1",
        "transaction_id": "tx1",
    }


def test_non_json_success_body_raises(call, router):
    router.get("/items/IT1", content=b"<html>")

    with pytest.raises(ILSError):
        call("get_item", "IT1")


def test_non_object_json_raises(call, router):
    router.get("/items/IT1", json=["not", "an", "object"])

    with pytest.raises(ILSError):
        call("get_item", "IT1")


@pytest.mark.parametrize(
    "answer", [{"status": 204}, {"content": b"<html>"}, {"json": ["not", "an", "object"]}]
)
def test_unreadable_2xx_on_a_write_is_ambiguous(call, router, answer):
    """The ILS answered 2xx, so it probably acted: the kiosk must not be told "no"."""
    router.post("/checkin", **answer)
    with pytest.raises(ILSAmbiguous):
        call("checkin", item_barcode="IT1", **CIRCULATION)


def test_empty_body_on_a_lookup_is_an_error(call, router):
    router.get("/items/IT1", status=200)
    with pytest.raises(ILSError):
        call("get_item", "IT1")


def test_204_is_fine_for_ping(call, router):
    router.get("/ping", status=204)
    assert call("ping") is True


def test_update_item_status_payload_shape(call, router):
    router.post("/items/A%2FB/status", json={"barcode": "A/B", "title": "T", "status": "other"})

    item = call("update_item_status", item_barcode="A/B", properties="seen", **CIRCULATION)

    assert item.title == "T"
    assert router.body() == {
        "item_barcode": "A/B",
        "properties": "seen",
        "user_pid": "42",
        "terminal": "kiosk1",
        "transaction_id": "tx1",
    }


# ------------------------------------------------------------------ parsing


def test_item_is_parsed_once_into_the_domain_type(call, router):
    router.get(
        "/items/IT1",
        json={
            "barcode": "IT1",
            "title": "SICP",
            "status": "on_loan",
            "location": "Main reading room",
            "current_location": "",
            "due_date": "2026-11-04",
            "hold_queue_length": "2",
            "owner": "CERN Library",
            "media_type": "video",
            "messages": ["Return to desk"],
        },
    )

    assert call("get_item", "IT1") == Item(
        barcode="IT1",
        title="SICP",
        status=ItemStatus.ON_LOAN,
        location="Main reading room",
        current_location=None,
        due_date=date(2026, 11, 4),
        hold_queue_length=2,
        owner="CERN Library",
        media_type=MediaType.VIDEO,
        messages=("Return to desk",),
    )


def test_minimal_item_gets_defaults(call, router):
    router.get("/items/IT1", json={"status": "available"})
    assert call("get_item", "IT1") == Item(barcode="IT1", title="", status=ItemStatus.AVAILABLE)


def test_unknown_status_becomes_other_with_a_warning(call, router, caplog):
    router.get("/items/IT1", json={"status": "teleported", "media_type": "hologram"})

    with caplog.at_level(logging.WARNING, logger="cds_sip2.rest.client"):
        item = call("get_item", "IT1")

    assert item.status is ItemStatus.OTHER
    assert item.media_type is None  # the SIP2 side falls back to its configured default
    assert "unknown ILS item status" in caplog.text
    assert "unknown ILS media type" in caplog.text


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-04", date(2026, 11, 4)),
        ("2026-11-04T23:59:00+01:00", date(2026, 11, 4)),
        ("tomorrow", None),
        (None, None),
        ("", None),
    ],
)
def test_due_dates(call, router, value, expected):
    router.get("/items/IT1", json={"status": "on_loan", "due_date": value})
    assert call("get_item", "IT1").due_date == expected


def test_messages_become_a_tuple(call, router):
    router.get("/items/IT1", json={"status": "available", "messages": "one"})
    assert call("get_item", "IT1").messages == ("one",)


def test_wrongly_shaped_lookup_body_is_an_ils_error_not_a_crash(call, router):
    router.get("/items/IT1", json={"status": "available", "messages": 42})
    with pytest.raises(ILSError):
        call("get_item", "IT1")


def test_checkout_and_checkin_results(call, router):
    router.post("/checkout", json={"title": "SICP", "due_date": "2026-11-11", "is_renewal": True})
    router.post("/checkin", json={"title": "SICP", "permanent_location": "Shelf 3", "alert": True})

    assert checkout(call) == CheckoutResult(
        title="SICP", due_date=date(2026, 11, 11), is_renewal=True
    )
    assert call("checkin", item_barcode="IT1", **CIRCULATION) == CheckinResult(
        title="SICP", permanent_location="Shelf 3", alert=True
    )
