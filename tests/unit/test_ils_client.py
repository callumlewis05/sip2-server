"""Unit tests for ``cds_sip2.ils.client`` (ILS mocked with ``httpx.MockTransport``)."""

import httpx
import pytest

from cds_sip2.ils import (
    ILSAmbiguous,
    ILSClient,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)
from tests.conftest import ILS_URL, run

CIRCULATION = {"user_pid": "42", "terminal": "kiosk1", "transaction_id": "tx1"}


@pytest.fixture
def call(router):
    """``call("get_item", "IT1")`` -> runs the client method against ``router``."""

    def _call(method, *args, **kwargs):
        async def go():
            client = ILSClient(
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

    assert item["status"] == "available"
    sent = router.calls[0]
    assert sent.headers["Authorization"] == "Bearer tok"
    assert sent.headers["Accept"] == "application/json"


def test_barcode_is_one_path_segment(call, router):
    router.get("/items/A%2FB%3F", json={"barcode": "A/B?"})
    assert call("get_item", "A/B?")["barcode"] == "A/B?"


def test_lookups_return_none_on_404(call, router):
    router.get("/items/NOPE", status=404)
    router.get("/patrons/NOPE", status=404)

    assert call("get_item", "NOPE") is None
    assert call("get_patron", "NOPE") is None


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
    router.post("/renew", status=status)

    with pytest.raises(ILSAmbiguous):
        call("renew", item_barcode="IT1", patron_barcode="P001", **CIRCULATION)


def test_ambiguous_is_not_unavailable():
    """Handlers must be able to tell the two apart with ``except`` order alone."""
    assert not issubclass(ILSAmbiguous, ILSUnavailable)


def test_login_and_verify_are_never_ambiguous(call, router):
    router.post("/login", exc=httpx.ReadTimeout)
    router.post("/patrons/P1/verify", status=500)

    with pytest.raises(ILSUnavailable):
        call("login", "kiosk1", "secret")
    with pytest.raises(ILSUnavailable):
        call("verify_patron_pin", "P1", "1234")


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


def test_login_refused_returns_none(call, router):
    router.post("/login", json={"ok": False})
    assert call("login", "kiosk1", "bad") is None


def test_login_ok_returns_payload(call, router):
    router.post("/login", json={"ok": True, "user_pid": "42", "location_pid": "1"})
    assert call("login", "kiosk1", "secret", ip="10.0.0.5")["user_pid"] == "42"
    assert router.body() == {"terminal": "kiosk1", "password": "secret", "ip": "10.0.0.5"}


def test_verify_pin(call, router):
    router.post("/patrons/P001/verify", json={"ok": True})
    router.post("/patrons/P002/verify", json={"ok": False})
    router.post("/patrons/NOPE/verify", status=404)

    assert call("verify_patron_pin", "P001", "1234") is True
    assert call("verify_patron_pin", "P002", "1234") is False
    assert call("verify_patron_pin", "NOPE", "1234") is False


def test_non_json_success_body_raises(call, router):
    router.get("/items/IT1", content=b"<html>")

    with pytest.raises(ILSError):
        call("get_item", "IT1")


def test_non_object_json_raises(call, router):
    router.get("/items/IT1", json=["not", "an", "object"])

    with pytest.raises(ILSError):
        call("get_item", "IT1")


def test_204_on_a_transaction_is_an_error(call, router):
    router.post("/checkin", status=204)
    with pytest.raises(ILSError):
        call("checkin", item_barcode="IT1", **CIRCULATION)


def test_204_is_fine_for_ping(call, router):
    router.get("/ping", status=204)
    assert call("ping") is True
