"""Async HTTP client for the ``/api/sip2`` endpoints exposed by CDS-ILS.

This module defines the HTTP contract between the SIP2 server and the ILS.
The fake ILS in ``tests/fake_ils.py`` implements the same contract.

Status-code semantics:

* ``2xx`` with a JSON object -> success, subject to endpoint-specific
  response validation.
* Empty ``2xx`` body -> empty dict for operations that permit it;
  an error for operations requiring a response body.
* ``404`` on GET patron/item lookups -> ``None``.
* ``404`` on a circulation POST -> ``ILSNotFound`` (unknown item).
* ``409`` / other ``4xx`` -> ``ILSRejected``, except ``401`` / ``403``.
* ``401`` / ``403`` -> our service token is wrong (``ILSConfigError``).
* Connect failure -> ``ILSUnavailable``: nothing was sent.
* Read timeout, reset or ``5xx`` -> ``ILSUnavailable`` on lookups,
  ``ILSAmbiguous`` on circulation POSTs (the ILS may have recorded it).
* Malformed JSON or a non-object JSON response -> ``ILSError``.

Circulation POSTs require a response body. An explicit ``{"ok": false}``
is treated as a transaction refusal.

Nothing here retries: a retried checkout is a double checkout. POST
bodies carry a ``transaction_id`` derived from the SIP2 request, and the
ILS must answer a duplicate ``transaction_id`` with the original result.
"""

import logging
from urllib.parse import quote

import httpx

from .errors import (
    ILSAmbiguous,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)

logger = logging.getLogger("cds_sip2.ils.client")

# The request never left this process, so the ILS cannot have acted on it.
_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


def _segment(value):
    """Encode a value as exactly one URL path segment."""
    return quote(str(value), safe="")


class ILSClient:
    """httpx-based client with one connection pool, bearer auth and strict timeouts."""

    def __init__(self, base_url, token, connect_timeout=3.0, read_timeout=8.0, transport=None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "cds-sip2",
            },
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
        )

    async def aclose(self):
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    def _url(self, path):
        return f"{self.base_url}/{path.lstrip('/')}"

    async def _request(
        self, method, path, json=None, not_found_ok=False, require_body=False, transaction_id=None
    ):
        """``transaction_id`` marks a circulation call, where a lost answer is ambiguous."""
        url = self._url(path)
        circulation = transaction_id is not None
        try:
            response = await self._client.request(method, url, json=json)
        except _NOT_SENT as exc:
            raise ILSUnavailable(f"{method} {path}: {exc.__class__.__name__}") from exc
        except httpx.TransportError as exc:
            detail = f"{method} {path}: {exc.__class__.__name__} after sending"
            if circulation:
                raise ILSAmbiguous(detail, transaction_id) from exc
            raise ILSUnavailable(detail) from exc
        except httpx.HTTPError as exc:
            raise ILSError(f"{method} {path}: {exc}") from exc

        status = response.status_code
        if status == 404 and not_found_ok:
            return None
        if status in (401, 403):
            raise ILSConfigError(f"{method} {path}: ILS rejected service token ({status})")
        if status == 404:
            raise ILSNotFound(f"{method} {path}: 404")
        if 400 <= status < 500:
            raise ILSRejected(status, _json_or_empty(response))
        if status >= 500:
            if circulation:
                raise ILSAmbiguous(f"{method} {path}: {status}", transaction_id)
            raise ILSUnavailable(f"{method} {path}: {status}")
        if not response.content:
            if require_body:
                raise ILSError(f"{method} {path}: empty response body")
            return {}

        try:
            result = response.json()
        except ValueError as exc:
            raise ILSError(f"{method} {path}: non-JSON response") from exc

        if not isinstance(result, dict):
            raise ILSError(f"{method} {path}: expected JSON object, got {type(result).__name__}")

        if require_body and result.get("ok") is False:
            raise ILSRejected(status, result)

        return result

    # ------------------------------------------------------------- endpoints

    async def ping(self):
        """``GET /ping`` -> bool. Never raises."""
        try:
            await self._request("GET", "/ping")
            return True
        except ILSError as exc:
            logger.warning("ILS ping failed: %s", exc, extra={"event": "ils.ping"})
            return False

    async def login(self, terminal, password, ip=None):
        """``POST /login`` -> dict with ``user_pid`` etc., or ``None`` if refused.

        Expected 200 body::

            {"ok": true, "user_pid": "42", "location_pid": "1",
             "institution_id": "CERN", "library_name": "CERN Library", "language": "en"}

        or ``{"ok": false}`` for bad terminal credentials.
        """
        payload = {"terminal": terminal, "password": password, "ip": ip}
        result = await self._request("POST", "/login", json=payload)
        return result if result and result.get("ok") else None

    async def get_patron(self, barcode):
        """``GET /patrons/<barcode>`` -> dict or ``None``.

        Expected body::

            {"barcode": "P001", "name": "Ada Lovelace", "email": "...",
             "status_flags": ["charge_privileges_denied", ...],   # PatronStatus values
             "loans": ["IT002"], "overdue": [], "holds": [], "recalls": [],
             "unavailable_holds": [], "fines": [],
             "fines_total": "0.00", "currency": "CHF"}
        """
        return await self._request("GET", f"/patrons/{_segment(barcode)}", not_found_ok=True)

    async def verify_patron_pin(self, barcode, pin):
        """``POST /patrons/<barcode>/verify`` ``{"pin": ...}`` -> bool."""
        result = await self._request(
            "POST", f"/patrons/{_segment(barcode)}/verify", json={"pin": pin}, not_found_ok=True
        )
        return bool(result and result.get("ok"))

    async def get_item(self, barcode):
        """``GET /items/<barcode>`` -> dict or ``None``.

        Expected body::

            {"barcode": "IT001", "title": "...",
             "status": "available|on_loan|recalled|on_hold_shelf|in_transit|in_process|
                        on_order|lost|missing|other",
             "location": "Main reading room", "current_location": null,
             "due_date": "2026-11-04", "hold_queue_length": 0, "owner": "CERN Library"}
        """
        return await self._request("GET", f"/items/{_segment(barcode)}", not_found_ok=True)

    async def checkout(self, *, item_barcode, patron_barcode, user_pid, terminal, transaction_id):
        """``POST /checkout`` -> ``{"title", "due_date", "is_renewal", ...}``.

        The ILS resolves the transaction location from ``terminal`` (its own
        terminal registry) and acts as ``user_pid``.
        """
        return await self._request(
            "POST",
            "/checkout",
            json={
                "item_barcode": item_barcode,
                "patron_barcode": patron_barcode,
                "user_pid": user_pid,
                "terminal": terminal,
                "transaction_id": transaction_id,
            },
            require_body=True,
            transaction_id=transaction_id,
        )

    async def checkin(self, *, item_barcode, user_pid, terminal, transaction_id):
        """``POST /checkin`` -> ``{"title", "permanent_location", "alert", ...}``."""
        return await self._request(
            "POST",
            "/checkin",
            json={
                "item_barcode": item_barcode,
                "user_pid": user_pid,
                "terminal": terminal,
                "transaction_id": transaction_id,
            },
            require_body=True,
            transaction_id=transaction_id,
        )

    async def renew(self, *, item_barcode, patron_barcode, user_pid, terminal, transaction_id):
        """``POST /renew`` -> ``{"title", "due_date", ...}``."""
        return await self._request(
            "POST",
            "/renew",
            json={
                "item_barcode": item_barcode,
                "patron_barcode": patron_barcode,
                "user_pid": user_pid,
                "terminal": terminal,
                "transaction_id": transaction_id,
            },
            require_body=True,
            transaction_id=transaction_id,
        )


def _json_or_empty(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
