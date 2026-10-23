"""Unit tests for ``cds_sip2.ils_client`` (ILS mocked with ``responses``)."""

import pytest
import requests
import responses

from cds_sip2.ils_client import (
    ILSClient,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)

BASE = "http://ils.test/api/sip2"


@pytest.fixture
def client():
    return ILSClient(BASE + "/", "tok", timeout=(0.5, 0.5))


@pytest.fixture
def mock():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def test_sends_bearer_token_and_accept_json(client, mock):
    mock.get(f"{BASE}/items/IT1", json={"barcode": "IT1", "status": "available"})

    item = client.get_item("IT1")

    assert item["status"] == "available"
    sent = mock.calls[0].request
    assert sent.headers["Authorization"] == "Bearer tok"
    assert sent.headers["Accept"] == "application/json"


def test_lookups_return_none_on_404(client, mock):
    mock.get(f"{BASE}/items/NOPE", status=404)
    mock.get(f"{BASE}/patrons/NOPE", status=404)

    assert client.get_item("NOPE") is None
    assert client.get_patron("NOPE") is None


def test_404_on_transaction_raises_not_found(client, mock):
    mock.post(f"{BASE}/checkout", status=404, json={"code": "item_not_found"})

    with pytest.raises(ILSNotFound):
        client.checkout("X", "P", "42", "kiosk1")


def test_5xx_raises_unavailable(client, mock):
    mock.get(f"{BASE}/items/IT1", status=503)

    with pytest.raises(ILSUnavailable):
        client.get_item("IT1")


def test_timeout_and_connection_errors_raise_unavailable(client, mock):
    mock.get(f"{BASE}/items/T", body=requests.exceptions.ConnectTimeout())
    mock.get(f"{BASE}/items/C", body=requests.exceptions.ConnectionError())

    with pytest.raises(ILSUnavailable):
        client.get_item("T")
    with pytest.raises(ILSUnavailable):
        client.get_item("C")


@pytest.mark.parametrize("status", [401, 403])
def test_service_token_rejection_raises_config_error(client, mock, status):
    mock.get(f"{BASE}/ping", status=status)
    mock.get(f"{BASE}/items/IT1", status=status)

    assert client.ping() is False  # ping never raises
    with pytest.raises(ILSConfigError):
        client.get_item("IT1")


def test_4xx_with_body_raises_rejected_with_user_message(client, mock):
    mock.post(
        f"{BASE}/checkout",
        status=409,
        json={"code": "item_not_available", "message": "status is on_loan",
              "user_message": "This item is not available.", "title": "SICP"},
    )

    with pytest.raises(ILSRejected) as info:
        client.checkout("IT1", "P001", "42", "kiosk1", transaction_id="abc")

    assert info.value.code == "item_not_available"
    assert info.value.user_message == "This item is not available."
    assert info.value.title == "SICP"
    assert info.value.status == 409


def test_4xx_without_json_body_still_rejected(client, mock):
    mock.post(f"{BASE}/checkin", status=400, body="nope")

    with pytest.raises(ILSRejected) as info:
        client.checkin("IT1", "42", "kiosk1")
    assert info.value.code == "rejected"


def test_posts_are_never_retried(client, mock):
    mock.post(f"{BASE}/checkout", status=500)

    with pytest.raises(ILSUnavailable):
        client.checkout("IT1", "P001", "42", "kiosk1")
    assert len(mock.calls) == 1


def test_checkout_payload_shape(client, mock):
    mock.post(f"{BASE}/checkout", json={"title": "SICP", "due_date": "2026-11-11"})

    client.checkout("IT1", "P001", "42", "kiosk1", transaction_id="tx1")

    import json

    body = json.loads(mock.calls[0].request.body)
    assert body == {"item_barcode": "IT1", "patron_barcode": "P001", "user_pid": "42",
                    "terminal": "kiosk1", "transaction_id": "tx1"}


def test_login_refused_returns_none(client, mock):
    mock.post(f"{BASE}/login", json={"ok": False})
    assert client.login("kiosk1", "bad") is None


def test_login_ok_returns_payload(client, mock):
    mock.post(f"{BASE}/login", json={"ok": True, "user_pid": "42", "location_pid": "1"})
    assert client.login("kiosk1", "secret", ip="10.0.0.5")["user_pid"] == "42"


def test_verify_pin(client, mock):
    mock.post(f"{BASE}/patrons/P001/verify", json={"ok": True})
    mock.post(f"{BASE}/patrons/P002/verify", json={"ok": False})
    mock.post(f"{BASE}/patrons/NOPE/verify", status=404)

    assert client.verify_patron_pin("P001", "1234") is True
    assert client.verify_patron_pin("P002", "1234") is False
    assert client.verify_patron_pin("NOPE", "1234") is False


def test_non_json_success_body_raises(client, mock):
    mock.get(f"{BASE}/items/IT1", body="<html>", content_type="text/html")

    with pytest.raises(ILSError):
        client.get_item("IT1")


def test_204_on_a_transaction_is_an_error(client, mock):
    mock.post(f"{BASE}/checkin", status=204)
    with pytest.raises(ILSError):
        client.checkin("IT1", "42", "kiosk1")


def test_204_is_fine_for_ping(client, mock):
    mock.get(f"{BASE}/ping", status=204)
    assert client.ping() is True