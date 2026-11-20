"""Typed SIP2 request -> ILSBackend call -> typed SIP2 response.

Design rules:

* Handlers never contain circulation policy. They validate nothing beyond
  "did the ILS say yes"; the ILS decides.
* Handlers never touch SIP2 tags or wire strings: they read the request
  classes and fill the response classes in :mod:`.mapper`, from the domain
  objects in :mod:`cds_sip2.domain`.
* Every outcome is a well-formed response. Refusals (unknown item, ILS
  declined) are logged at INFO; failures (ILS down, ambiguous, bad token) at
  ERROR, with ``alert`` set for the ones an operator must act on.
* Only :mod:`cds_sip2.errors` exceptions are handled here. Anything else is a
  bug, and the dispatcher turns it into a generic refusal.

Supported: 93 Login, 99 SC Status, 17 Item Information, 11 Checkout,
09 Checkin and 19 Item Status Update. Every other known message is refused
by the dispatcher as unsupported, and the 98 advertises exactly ``routes``.
"""

import hmac
import logging

from ..errors import ILSAmbiguous, ILSConfigError, ILSError, ILSNotFound, ILSRejected
from ..logging import pseudonymise
from ..utils import to_sip2_date
from . import enums, mapper
from .mapper import (
    ACSStatus,
    CheckinResponse,
    CheckoutResponse,
    ItemInformationResponse,
    ItemStatusUpdateResponse,
    LoginResponse,
    Reply,
)
from .messages import supported_messages

logger = logging.getLogger("cds_sip2.sip2.handlers")

_NO_TERMINAL = {"password": "\x00" * 32}
"""Compared against when CN is unknown, so timing does not reveal valid CNs."""


class Handlers:
    def __init__(self, ils, settings):
        self.ils = ils
        self.settings = settings
        self.routes = {
            "93": self.login,
            "99": self.sc_status,
            "17": self.item_information,
            "11": self.checkout,
            "09": self.checkin,
            "19": self.item_status_update,
        }
        """The single source of truth for what is supported; the 98's BX
        field and capability flags are derived from it."""

    @property
    def supported(self):
        return set(self.routes) | {"97"}

    # ------------------------------------------------------------- helpers

    def _institution(self, institution_id, session):
        return institution_id or session.institution_id or self.settings.institution_id

    def _language(self, session):
        return enums.language_code(session.language or self.settings.language)

    def _media_type(self, media_type):
        if media_type is None:
            return self.settings.default_media_type
        return enums.MEDIA_TYPE[media_type]

    def _failure(self, event, exc, **context):
        """Log an ILS failure; return the screen-message key for the kiosk."""
        if isinstance(exc, ILSAmbiguous):
            logger.error(
                "%s outcome unknown: %s",
                event,
                exc,
                extra={
                    "event": event,
                    "outcome": "ambiguous",
                    "transaction_id": exc.transaction_id,
                    **context,
                },
            )
            return "ambiguous"
        alert = isinstance(exc, ILSConfigError)
        logger.error(
            "%s failed: %s",
            event,
            exc,
            extra={"event": event, "outcome": "error", "alert": alert, **context},
        )
        return "unavailable"

    # ------------------------------------------------------------ refusals

    def refuse(self, request, session, key, *, message=None, replayable=True, **overrides):
        """A well-formed negative reply to any known request code.

        ``key`` picks the configured screen message unless ``message`` (an ILS
        ``user_message``) is given. ``overrides`` set response attributes.
        """
        text = message or self.settings.message(key)
        if request.code == "99":
            return Reply(self._acs_status(session, online=False), replayable)
        refusal = mapper.refuse(
            request,
            institution_id=self._institution(request.get("AO"), session),
            language=self._language(session),
            message=text,
            **overrides,
        )
        return refusal.reply(replayable)

    async def _write(self, event, call, message, session, **context):
        """Run an ILS write; return ``(result, None)`` or ``(None, refusal)``."""
        request = message.source
        try:
            return await call(), None
        except ILSNotFound:
            logger.info(
                "%s refused: unknown item",
                event,
                extra={"event": event, "outcome": "unknown_item", **context},
            )
            return None, self.refuse(request, session, "unknown_item")
        except ILSRejected as exc:
            logger.info(
                "%s refused",
                event,
                extra={"event": event, "outcome": "refused", "code": exc.code, **context},
            )
            overrides = {"title": exc.title} if exc.title else {}
            return None, self.refuse(
                request, session, "refused", message=exc.user_message, **overrides
            )
        except ILSError as exc:
            key = self._failure(event, exc, **context)
            return None, self.refuse(request, session, key, replayable=key != "ambiguous")

    # ------------------------------------------------------ login / status

    async def login(self, message, session, caller):
        """93 Login -> 94, checked against ``CDS_SIP2_TERMINALS``.

        Stores the terminal and its ``user_pid`` on the session.
        """
        session.logout()
        context = {"event": "login", "terminal": message.terminal}
        entry = self.settings.terminals.get(message.terminal)
        expected = (entry or _NO_TERMINAL)["password"]
        matches = hmac.compare_digest(expected.encode(), message.password.encode())
        if entry is None or not matches:
            session.failed_logins += 1
            logger.warning(
                "login refused",
                extra={**context, "outcome": "refused", "attempt": session.failed_logins},
            )
            return LoginResponse(ok=False).reply()

        settings = self.settings
        session.login(
            terminal=message.terminal,
            user_pid=str(entry["user_pid"]),
            institution_id=settings.institution_id,
            library_name=settings.library_name,
            language=settings.language,
        )
        logger.info("login ok", extra={**context, "outcome": "ok"})
        return LoginResponse(ok=True).reply()

    async def sc_status(self, message, session, caller):
        """99 SC Status -> 98 ACS Status. Allowed before login."""
        online = await self.ils.ping()
        if not online:
            logger.warning(
                "ILS not reachable", extra={"event": "sc_status", "terminal": session.terminal}
            )
        return Reply(self._acs_status(session, online))

    def _acs_status(self, session, online):
        supported = self.supported
        settings = self.settings
        return ACSStatus(
            online_status=online,
            checkin_ok="09" in supported,
            checkout_ok="11" in supported,
            acs_renewal_policy="29" in supported,
            timeout_period=settings.advertised_timeout,
            retries_allowed=f"{settings.retries_allowed:03d}",
            institution_id=session.institution_id or settings.institution_id,
            library_name=session.library_name or settings.library_name,
            supported_messages=supported_messages(supported),
            screen_message=None if online else settings.message("unavailable"),
        ).to_response()

    # ---------------------------------------------------------------- items

    async def item_information(self, message, session, caller):
        """17 Item Information -> 18."""
        barcode = message.item_barcode
        try:
            item = await self.ils.get_item(barcode)
        except ILSError as exc:
            key = self._failure("item_information", exc, item=barcode)
            return self.refuse(message.source, session, key)
        if item is None:
            logger.info(
                "unknown item",
                extra={"event": "item_information", "outcome": "unknown_item", "item": barcode},
            )
            return self.refuse(message.source, session, "unknown_item")

        return ItemInformationResponse(
            circulation_status=enums.CIRCULATION_STATUS[item.status],
            security_marker=self.settings.security_marker,
            item_barcode=barcode,
            title=item.title,
            media_type=self._media_type(item.media_type),
            due_date=to_sip2_date(item.due_date),
            permanent_location=item.location,
            current_location=item.current_location or item.location,
            hold_queue_length=item.hold_queue_length,
            owner=item.owner,
            screen_message=item.messages or None,
        ).reply()

    # ---------------------------------------------------------------- writes

    async def checkout(self, message, session, caller):
        """11 Checkout -> 12."""
        item, patron = message.item_barcode, message.patron_barcode
        context = {
            "item": item,
            "patron": pseudonymise(patron),
            "terminal": caller.terminal,
            "transaction_id": caller.transaction_id,
        }
        result, refusal = await self._write(
            "checkout",
            lambda: self.ils.checkout(item_barcode=item, patron_barcode=patron, caller=caller),
            message,
            session,
            **context,
        )
        if refusal:
            return refusal
        logger.info(
            "checkout ok",
            extra={"event": "checkout", "outcome": "ok", "due": result.due_date, **context},
        )
        return CheckoutResponse(
            ok=True,
            renewal_ok=result.is_renewal,
            desensitize=True,
            institution_id=self._institution(message.institution_id, session),
            patron_barcode=patron,
            item_barcode=item,
            title=result.title,
            due_date=to_sip2_date(result.due_date) or "",
            media_type=self._media_type(result.media_type),
            screen_message=result.messages or None,
        ).reply()

    async def checkin(self, message, session, caller):
        """09 Checkin -> 10."""
        item = message.item_barcode
        context = {
            "item": item,
            "terminal": caller.terminal,
            "transaction_id": caller.transaction_id,
        }
        result, refusal = await self._write(
            "checkin",
            lambda: self.ils.checkin(item_barcode=item, caller=caller),
            message,
            session,
            **context,
        )
        if refusal:
            return refusal
        logger.info("checkin ok", extra={"event": "checkin", "outcome": "ok", **context})
        return CheckinResponse(
            ok=True,
            resensitize=True,
            # alert=Y makes the device beep: e.g. the item has a pending hold
            # and should go to the desk rather than the shelf.
            alert=result.alert,
            institution_id=self._institution(message.institution_id, session),
            item_barcode=item,
            permanent_location=result.permanent_location or "",
            title=result.title,
            media_type=self._media_type(result.media_type),
            screen_message=result.messages or None,
        ).reply()

    async def item_status_update(self, message, session, caller):
        """19 Item Status Update -> 20. ``CH`` is passed to the ILS verbatim."""
        barcode = message.item_barcode
        context = {
            "item": barcode,
            "terminal": caller.terminal,
            "transaction_id": caller.transaction_id,
        }
        item, refusal = await self._write(
            "item_status_update",
            lambda: self.ils.update_item_status(
                item_barcode=barcode, properties=message.properties, caller=caller
            ),
            message,
            session,
            **context,
        )
        if refusal:
            return refusal
        logger.info(
            "item status update ok",
            extra={"event": "item_status_update", "outcome": "ok", **context},
        )
        return ItemStatusUpdateResponse(
            item_properties_ok=True,
            item_barcode=barcode,
            title=item.title,
            screen_message=item.messages or None,
        ).reply()
