"""Thin HTTP client for the ``/api/sip2`` endpoints exposed by CDS-ILS.

This module defines the HTTP contract between the sidecar and the ILS.
The fake ILS in ``tests/fake_ils.py`` implements the same contract.

Status-code semantics:

* ``2xx`` with a JSON object -> success, subject to endpoint-specific
  response validation.
* Empty ``2xx`` body -> empty dict for operations that permit it;
  an error for operations requiring a response body.
* ``404`` on GET patron/item lookups -> ``None``.
* ``404`` on POST or other unexpected endpoints -> ``ILSNotFound``.
* ``409`` / other ``4xx`` -> ``ILSRejected``, except ``401`` / ``403``.
* ``401`` / ``403`` -> our service token is wrong (``ILSConfigError``).
* ``5xx`` and connection/timeouts -> ``ILSUnavailable``.
* Malformed JSON or a non-object JSON response -> ``ILSError``.

Circulation POSTs require a response body. An explicit ``{"ok": false}``
is treated as a transaction refusal.

Nothing here retries: a retried checkout is a double checkout. POST
bodies carry a ``transaction_id`` so the ILS can make them idempotent
if it wants.
"""

import logging

import requests

logger = logging.getLogger("cds_sip2.ils_client")

from urllib.parse import quote


def _segment(value):
    """Encode a value as exactly one URL path segment."""
    return quote(str(value), safe="")


class ILSError(Exception):
    """Base class for all ILS client errors."""


class ILSUnavailable(ILSError):
    """The ILS could not be reached, timed out, or answered 5xx."""


class ILSConfigError(ILSError):
    """The ILS rejected the sidecar's own credentials (401/403). Page an operator."""


class ILSNotFound(ILSError):
    """404 on an endpoint where "not found" is not an expected outcome."""


class ILSRejected(ILSError):
    """The ILS refused the transaction (4xx with a JSON error body)."""

    def __init__(self, status, payload):
        self.status = status
        self.payload = payload or {}
        self.code = self.payload.get("code", "rejected")
        self.message = self.payload.get("message", "")
        self.user_message = (
            self.payload.get("user_message") or self.message or "Transaction refused"
        )
        self.title = self.payload.get("title")
        super().__init__(f"{status} {self.code}: {self.message}")


class ILSClient:
    """Requests-based client with one session, bearer auth and strict timeouts."""

    def __init__(self, base_url, token, timeout=(3.0, 8.0), session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "cds-sip2",
            }
        )

    # ------------------------------------------------------------------ core

    def _url(self, path):
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method, path, json=None, not_found_ok=False, require_body=False):
        url = self._url(path)
        try:
            response = self.session.request(method, url, json=json, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ILSUnavailable(f"{method} {url}: {exc.__class__.__name__}") from exc
        except requests.RequestException as exc:
            raise ILSError(f"{method} {url}: {exc}") from exc

        status = response.status_code
        if status == 404 and not_found_ok:
            return None
        if status in (401, 403):
            raise ILSConfigError(f"{method} {url}: ILS rejected service token ({status})")
        if status == 404:
            raise ILSNotFound(f"{method} {url}: 404")
        if 400 <= status < 500:
            raise ILSRejected(status, _json_or_empty(response))
        if status >= 500:
            raise ILSUnavailable(f"{method} {url}: {status}")
        if not response.content:
            if require_body:
                raise ILSError(
                    f"{method} {url}: empty response body"
                )
            if status == 204:
                return {}
            return {}

        try:
            result = response.json()
        except ValueError as exc:
            raise ILSError(
                f"{method} {url}: non-JSON response"
            ) from exc

        if not isinstance(result, dict):
            raise ILSError(
                f"{method} {url}: expected JSON object, "
                f"got {type(result).__name__}"
            )

        if require_body and result.get("ok") is False:
            raise ILSRejected(status, result)

        return result

    # ------------------------------------------------------------- endpoints

    def ping(self):
        """``GET /ping`` -> bool. Never raises."""
        try:
            self._request("GET", "/ping")
            return True
        except ILSError as exc:
            logger.warning("ILS ping failed: %s", exc)
            return False

    def login(self, terminal, password, ip=None):
        """``POST /login`` -> dict with ``user_pid`` etc., or ``None`` if refused.

        Expected 200 body::

            {"ok": true, "user_pid": "42", "location_pid": "1",
             "institution_id": "CERN", "library_name": "CERN Library", "language": "en"}

        or ``{"ok": false}`` for bad terminal credentials.
        """
        payload = {"terminal": terminal, "password": password, "ip": ip}
        result = self._request("POST", "/login", json=payload)
        return result if result and result.get("ok") else None

    def get_patron(self, barcode):
        """``GET /patrons/<barcode>`` -> dict or ``None``.

        Expected body::

            {"barcode": "P001", "name": "Ada Lovelace", "email": "...",
             "status_flags": ["charge_privileges_denied", ...],   # PatronStatusTypes values
             "loans": ["IT002"], "overdue": [], "holds": [], "recalls": [],
             "unavailable_holds": [], "fines": [],
             "fines_total": "0.00", "currency": "CHF"}
        """
        return self._request(
            "GET",
            f"/patrons/{_segment(barcode)}",
            not_found_ok=True,
        )

    def verify_patron_pin(self, barcode, pin):
        """``POST /patrons/<barcode>/verify`` ``{"pin": ...}`` -> bool."""
        result = self._request(
            "POST",
            f"/patrons/{_segment(barcode)}/verify",
            json={"pin": pin},
            not_found_ok=True
        )
        return bool(result and result.get("ok"))

    def get_item(self, barcode):
        """``GET /items/<barcode>`` -> dict or ``None``.

        Expected body::

            {"barcode": "IT001", "title": "...",
             "status": "available|on_loan|recalled|on_hold_shelf|in_transit|in_process|
                        on_order|lost|missing|other",
             "location": "Main reading room", "current_location": null,
             "due_date": "2026-11-04", "hold_queue_length": 0, "owner": "CERN Library"}
        """
        return self._request(
            "GET",
            f"/items/{_segment(barcode)}",
            not_found_ok=True,
        )

    def checkout(self, item_barcode, patron_barcode, user_pid, terminal, transaction_id=None):
        """``POST /checkout`` -> ``{"title", "due_date", "is_renewal", ...}``.

        The ILS resolves the transaction location from ``terminal`` (its own
        terminal registry) and acts as ``user_pid``.
        """
        return self._request("POST", "/checkout", json={
            "item_barcode": item_barcode,
            "patron_barcode": patron_barcode,
            "user_pid": user_pid,
            "terminal": terminal,
            "transaction_id": transaction_id,
        }, require_body=True)

    def checkin(self, item_barcode, user_pid, terminal, transaction_id=None):
        """``POST /checkin`` -> ``{"title", "permanent_location", "alert", ...}``."""
        return self._request("POST", "/checkin", json={
            "item_barcode": item_barcode,
            "user_pid": user_pid,
            "terminal": terminal,
            "transaction_id": transaction_id,
        }, require_body=True)

    def renew(self, item_barcode, patron_barcode, user_pid, terminal, transaction_id=None):
        """``POST /renew`` -> ``{"title", "due_date", ...}``."""
        return self._request("POST", "/renew", json={
            "item_barcode": item_barcode,
            "patron_barcode": patron_barcode,
            "user_pid": user_pid,
            "terminal": terminal,
            "transaction_id": transaction_id,
        }, require_body=True)


def _json_or_empty(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
