"""Async HTTP client for the ``/api/sip2`` endpoints exposed by CDS-ILS.

This module defines the HTTP contract between the SIP2 server and the ILS,
and is the only place that knows the payload shapes: every response is
parsed here, once, into the types in :mod:`cds_sip2.domain`. The fake ILS in
``tests/fake_ils.py`` implements the same contract.

Every request carries ``Authorization: Bearer <CDS_SIP2_ILS_TOKEN>``; the
token identifies this service to the ILS.

Status-code semantics:

* ``2xx`` with a JSON object -> success, parsed into a domain object.
* Empty ``2xx`` body -> fine for ``ping``; an error everywhere else.
* ``404`` on ``GET /items/<barcode>`` -> ``None``.
* ``404`` on a write -> ``ILSNotFound`` (unknown item).
* ``409`` / other ``4xx`` -> ``ILSRejected``, except ``401`` / ``403``.
* ``401`` / ``403`` -> our service token is wrong (``ILSConfigError``).
* Connect failure -> ``ILSUnavailable``: nothing was sent.
* Read timeout, reset or ``5xx`` -> ``ILSUnavailable`` on lookups,
  ``ILSAmbiguous`` on writes (the ILS may have recorded it).
* Malformed or unparseable body -> ``ILSError`` on lookups, ``ILSAmbiguous``
  on writes (the ILS answered 2xx, so it probably did act).

An explicit ``{"ok": false}`` on a write is treated as a refusal.

Nothing here retries: a retried checkout is a double checkout. Write bodies
carry ``terminal``, ``user_pid`` and ``transaction_id`` from the
:class:`~cds_sip2.domain.Caller`, and the ILS must answer a duplicate
``transaction_id`` with the original result.
"""

import logging
from datetime import date, datetime
from urllib.parse import quote

import httpx

from ..domain import CheckinResult, CheckoutResult, Item, ItemStatus, MediaType
from ..errors import (
    ILSAmbiguous,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
    ILSUnavailable,
)

logger = logging.getLogger("cds_sip2.rest.client")

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

    async def _request(self, method, path, json=None, not_found_ok=False, transaction_id=None):
        """One HTTP exchange -> JSON object, ``None`` (404 when allowed) or ``{}``.

        ``transaction_id`` marks a write, where a lost answer is ambiguous.
        """
        url = self._url(path)
        write = transaction_id is not None
        try:
            response = await self._client.request(method, url, json=json)
        except _NOT_SENT as exc:
            raise ILSUnavailable(f"{method} {path}: {exc.__class__.__name__}") from exc
        except httpx.TransportError as exc:
            detail = f"{method} {path}: {exc.__class__.__name__} after sending"
            if write:
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
            if write:
                raise ILSAmbiguous(f"{method} {path}: {status}", transaction_id)
            raise ILSUnavailable(f"{method} {path}: {status}")
        if not response.content:
            if write:
                raise ILSAmbiguous(f"{method} {path}: empty response body", transaction_id)
            return {}

        try:
            result = response.json()
        except ValueError:
            result = None
        if not isinstance(result, dict):
            detail = f"{method} {path}: expected a JSON object"
            if write:
                raise ILSAmbiguous(detail, transaction_id)
            raise ILSError(detail)
        if write and result.get("ok") is False:
            raise ILSRejected(status, result)
        return result

    async def _write(self, path, parse, caller, **body):
        payload = {
            **body,
            "user_pid": caller.user_pid,
            "terminal": caller.terminal,
            "transaction_id": caller.transaction_id,
        }
        result = await self._request(
            "POST", path, json=payload, transaction_id=caller.transaction_id
        )
        try:
            return parse(result, body.get("item_barcode", ""))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ILSAmbiguous(f"POST {path}: unreadable body ({exc})", caller.transaction_id)

    # ------------------------------------------------------------- endpoints

    async def ping(self):
        """``GET /ping`` -> bool. Never raises."""
        try:
            await self._request("GET", "/ping")
            return True
        except ILSError as exc:
            logger.warning("ILS ping failed: %s", exc, extra={"event": "ils.ping"})
            return False

    async def get_item(self, barcode):
        """``GET /items/<barcode>`` -> :class:`Item` or ``None``.

        Expected body (only ``status`` is required)::

            {"barcode": "IT001", "title": "...",
             "status": "available|on_loan|recalled|on_hold_shelf|in_transit|
                        in_process|on_order|lost|missing|other",
             "location": "Main reading room", "current_location": null,
             "due_date": "2026-11-04", "hold_queue_length": 0,
             "owner": "CERN Library", "media_type": "book", "messages": []}

        ``media_type`` is one of the :class:`~cds_sip2.domain.MediaType` values.
        """
        result = await self._request("GET", f"/items/{_segment(barcode)}", not_found_ok=True)
        if result is None:
            return None
        if not result:
            raise ILSError(f"GET /items/{barcode}: empty response body")
        try:
            return _item(result, barcode)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ILSError(f"GET /items/{barcode}: unreadable body ({exc})") from exc

    async def checkout(self, *, item_barcode, patron_barcode, caller):
        """``POST /checkout`` -> :class:`CheckoutResult`.

        Body: ``{"item_barcode", "patron_barcode", "user_pid", "terminal",
        "transaction_id"}``. Answer: ``{"title", "due_date", "is_renewal",
        "media_type", "messages"}``. The ILS resolves the transaction location
        from ``terminal`` and acts as ``user_pid``.
        """
        return await self._write(
            "/checkout",
            _checkout_result,
            caller,
            item_barcode=item_barcode,
            patron_barcode=patron_barcode,
        )

    async def checkin(self, *, item_barcode, caller):
        """``POST /checkin`` -> :class:`CheckinResult`.

        Body: ``{"item_barcode", "user_pid", "terminal", "transaction_id"}``.
        Answer: ``{"title", "permanent_location", "alert", "media_type",
        "messages"}``.
        """
        return await self._write("/checkin", _checkin_result, caller, item_barcode=item_barcode)

    async def update_item_status(self, *, item_barcode, properties, caller):
        """``POST /items/<barcode>/status`` -> :class:`Item` (after the update).

        Body: ``{"item_barcode", "properties", "user_pid", "terminal",
        "transaction_id"}``. ``properties`` is the SIP2 ``CH`` value from a
        19 Item Status Update, passed through verbatim: its meaning is agreed
        between the ILS and the device (for an inventory wand, typically "seen
        at <location>"). Answer: the item, as for ``GET /items/<barcode>``.
        """
        return await self._write(
            f"/items/{_segment(item_barcode)}/status",
            _item,
            caller,
            item_barcode=item_barcode,
            properties=properties,
        )


# ---------------------------------------------------------------- parsing
#
# Lenient on optional fields (log and drop), strict on the shape: a non-dict
# or a field of the wrong kind raises TypeError / ValueError, which the
# callers turn into ILSError (lookups) or ILSAmbiguous (writes).


def _item(payload, barcode):
    return Item(
        barcode=_str(payload.get("barcode")) or barcode,
        title=_str(payload.get("title")),
        status=_item_status(payload.get("status"), barcode),
        location=_optional_str(payload.get("location")),
        current_location=_optional_str(payload.get("current_location")),
        due_date=_date(payload.get("due_date"), "due_date", barcode),
        hold_queue_length=_int(payload.get("hold_queue_length"), "hold_queue_length", barcode),
        owner=_optional_str(payload.get("owner")),
        media_type=_media_type(payload.get("media_type"), barcode),
        messages=_messages(payload.get("messages")),
    )


def _checkout_result(payload, barcode):
    return CheckoutResult(
        title=_str(payload.get("title")),
        due_date=_date(payload.get("due_date"), "due_date", barcode),
        is_renewal=bool(payload.get("is_renewal")),
        media_type=_media_type(payload.get("media_type"), barcode),
        messages=_messages(payload.get("messages")),
    )


def _checkin_result(payload, barcode):
    return CheckinResult(
        title=_str(payload.get("title")),
        permanent_location=_optional_str(payload.get("permanent_location")),
        alert=bool(payload.get("alert")),
        media_type=_media_type(payload.get("media_type"), barcode),
        messages=_messages(payload.get("messages")),
    )


def _str(value):
    return "" if value is None else str(value)


def _optional_str(value):
    return None if value in (None, "") else str(value)


def _item_status(value, barcode):
    try:
        return ItemStatus(value)
    except ValueError:
        logger.warning(
            "unknown ILS item status, using 'other'",
            extra={"event": "ils.parse", "status": value, "item": barcode},
        )
        return ItemStatus.OTHER


def _media_type(value, barcode):
    if value in (None, ""):
        return None
    try:
        return MediaType(value)
    except ValueError:
        logger.warning(
            "unknown ILS media type, using the default",
            extra={"event": "ils.parse", "media_type": value, "item": barcode},
        )
        return None


def _date(value, name, barcode):
    """ISO date or datetime -> ``date``; unparseable -> ``None`` with a warning."""
    if value in (None, ""):
        return None
    try:
        text = str(value)
        return datetime.fromisoformat(text).date() if "T" in text else date.fromisoformat(text)
    except ValueError:
        logger.warning(
            "unparseable ILS date, dropping it",
            extra={"event": "ils.parse", "field": name, "value": value, "item": barcode},
        )
        return None


def _int(value, name, barcode):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        logger.warning(
            "non-integer ILS value, dropping it",
            extra={"event": "ils.parse", "field": name, "value": value, "item": barcode},
        )
        return None


def _messages(value):
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(message) for message in value)


def _json_or_empty(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
