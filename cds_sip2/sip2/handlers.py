"""SIP2 request -> ILSBackend call -> SIP2 response.

Design rules:

* Handlers never contain circulation policy. They validate nothing beyond
  "did the ILS say yes"; the ILS decides.
* Every outcome is a well-formed response. Refusals (unknown item, ILS
  declined) are logged at INFO; failures (ILS down, ambiguous, bad token) at
  ERROR, with ``alert`` set for the ones an operator must act on.
* Only :mod:`cds_sip2.ils` exceptions are handled here. Anything else is a
  bug, and the dispatcher turns it into a generic refusal.
"""

import logging

from ..ils.errors import (
    ILSAmbiguous,
    ILSConfigError,
    ILSError,
    ILSNotFound,
    ILSRejected,
)
from ..logging import pseudonymise
from ..utils import compact, normalize_barcode, sip2_now, to_sip2_date
from . import enums
from .codec import build
from .messages import REPLY_CODE, SUMMARY_LISTS, supported_messages

logger = logging.getLogger("cds_sip2.sip2.handlers")

DENIED_STATUS = enums.patron_status(enums.DENIED_FLAGS)[0]


def _yn(value):
    return "Y" if value else "N"


def _count(items):
    return f"{min(len(items or ()), 9999):04d}"


class Handlers:
    def __init__(self, ils, settings):
        self.ils = ils
        self.settings = settings
        self.routes = {
            "93": self.login,
            "99": self.sc_status,
            "23": self.patron_status,
            "25": self.patron_enable,
            "63": self.patron_information,
            "17": self.item_information,
            "11": self.checkout,
            "09": self.checkin,
            "29": self.renew,
            "35": self.end_patron_session,
        }
        """The single source of truth for what is supported; the 98's BX
        field and capability flags are derived from it."""

    @property
    def supported(self):
        return set(self.routes) | {"97"}

    # ------------------------------------------------------------- helpers

    def _msg(self, key):
        return self.settings.message(key)

    def _institution(self, request, session):
        return request.get("AO") or session.institution_id or self.settings.institution_id

    def _language(self, session):
        return enums.language_code(session.language or self.settings.language)

    def _patron_status(self, patron, event):
        field, unknown = enums.patron_status(patron.get("status_flags"))
        if unknown:
            logger.warning(
                "ILS sent unknown patron status flags; ignoring",
                extra={"event": event, "flags": unknown},
            )
        return field

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

    async def _lookup_patron(self, barcode, event):
        """``(patron | None, screen message | None)``. Never raises ILSError."""
        try:
            patron = await self.ils.get_patron(barcode)
        except ILSError as exc:
            return None, self._msg(self._failure(event, exc, patron=pseudonymise(barcode)))
        if patron is None:
            logger.info(
                "%s: unknown patron",
                event,
                extra={
                    "event": event,
                    "outcome": "unknown_patron",
                    "patron": pseudonymise(barcode),
                },
            )
            return None, self._msg("unknown_patron")
        return patron, None

    async def _verify_pin(self, request, barcode, event):
        """``CQ`` value, or ``None`` when the device sent no PIN (no AD field).

        An empty AD is still passed to the ILS: whether a PIN is required is
        policy.
        """
        if not request.has("AD"):
            return None
        try:
            return _yn(await self.ils.verify_patron_pin(barcode, request.get("AD")))
        except ILSError as exc:
            self._failure(event, exc, patron=pseudonymise(barcode))
            return "N"

    # ------------------------------------------------------ login / status

    async def login(self, request, session):
        """93 Login -> 94. Stores terminal and user_pid on the session."""
        terminal = request.get("CN")
        session.logout()
        context = {"event": "login", "terminal": terminal}
        try:
            result = await self.ils.login(terminal, request.get("CO"), ip=session.peer or None)
        except ILSError as exc:
            self._failure("login", exc, terminal=terminal)
            return build("94", {"ok": "0"})
        if not result:
            session.failed_logins += 1
            logger.warning(
                "login refused",
                extra={**context, "outcome": "refused", "attempt": session.failed_logins},
            )
            return build("94", {"ok": "0"})
        if result.get("user_pid") in (None, ""):
            logger.error(
                "ILS accepted terminal but sent no user_pid; refusing",
                extra={**context, "outcome": "error", "alert": True},
            )
            return build("94", {"ok": "0"})

        session.login(
            terminal=terminal,
            user_pid=str(result["user_pid"]),
            institution_id=result.get("institution_id") or self.settings.institution_id,
            library_name=result.get("library_name") or self.settings.library_name,
            language=result.get("language") or self.settings.language,
        )
        logger.info("login ok", extra={**context, "outcome": "ok"})
        return build("94", {"ok": "1"})

    async def sc_status(self, request, session):
        """99 SC Status -> 98 ACS Status. Allowed before login."""
        online = await self.ils.ping()
        if not online:
            logger.warning(
                "ILS not reachable", extra={"event": "sc_status", "terminal": session.terminal}
            )
        return self._acs_status(session, online)

    def _acs_status(self, session, online):
        supported = self.supported
        return build(
            "98",
            {
                "online_status": _yn(online),
                "checkin_ok": _yn("09" in supported),
                "checkout_ok": _yn("11" in supported),
                "acs_renewal_policy": _yn("29" in supported),
                "status_update_ok": "N",
                "offline_ok": "N",
                "timeout_period": f"{self.settings.timeout_period:03d}",
                "retries_allowed": f"{self.settings.retries_allowed:03d}",
                "date_time_sync": sip2_now(),
                "protocol_version": "2.00",
            },
            AO=session.institution_id or self.settings.institution_id,
            AM=session.library_name or self.settings.library_name,
            BX=supported_messages(supported),
            AF=None if online else self._msg("unavailable"),
        )

    # -------------------------------------------------------------- patrons

    async def _patron_reply(self, code, event, request, session):
        """Shared body of 23 -> 24 and 25 -> 26."""
        barcode = normalize_barcode(request.get("AA"))
        patron, message = await self._lookup_patron(barcode, event)
        fixed = {
            "patron_status": self._patron_status(patron, event) if patron else DENIED_STATUS,
            "language": self._language(session),
            "transaction_date": sip2_now(),
        }
        return build(
            code,
            fixed,
            AO=self._institution(request, session),
            AA=barcode,
            AE=(patron or {}).get("name", ""),
            BL=_yn(patron),
            CQ=await self._verify_pin(request, barcode, event) if patron else None,
            AF=message,
        )

    async def patron_status(self, request, session):
        """23 Patron Status Request -> 24."""
        return await self._patron_reply("24", "patron_status", request, session)

    async def patron_enable(self, request, session):
        """25 Patron Enable -> 26.

        Kiosks cannot lift blocks in CDS-ILS; we simply report the current status.
        """
        return await self._patron_reply("26", "patron_enable", request, session)

    async def patron_information(self, request, session):
        """63 Patron Information -> 64.

        The six 4-digit counts come from the payload's lists. The one list
        the device asked for in ``summary`` is also sent as repeated fields,
        sliced by BP/BQ (1-based, inclusive).
        """
        barcode = normalize_barcode(request.get("AA"))
        patron, message = await self._lookup_patron(barcode, "patron_information")
        fixed = {"language": self._language(session), "transaction_date": sip2_now()}
        common = {"AO": self._institution(request, session), "AA": barcode}
        if patron is None:
            return build(
                "64",
                {
                    **fixed,
                    "patron_status": DENIED_STATUS,
                    **{f"{name}_count": "0000" for name in _COUNT_NAMES},
                },
                **common,
                AE="",
                BL="N",
                AF=message,
            )

        lists = [patron.get(key) or [] for key in _LIST_KEYS]
        fines_total = patron.get("fines_total")
        has_fines = bool(fines_total) and str(fines_total) not in ("0", "0.0", "0.00")
        return build(
            "64",
            {
                **fixed,
                "patron_status": self._patron_status(patron, "patron_information"),
                **{f"{name}_count": _count(items) for name, items in zip(_COUNT_NAMES, lists)},
            },
            **common,
            AE=patron.get("name", ""),
            BL="Y",
            CQ=await self._verify_pin(request, barcode, "patron_information"),
            **compact(
                BV=str(fines_total) if has_fines else None,
                BH=patron.get("currency") if has_fines else None,
                BD=patron.get("address"),
                BE=patron.get("email"),
                BF=patron.get("phone"),
                AF=patron.get("messages") or None,
            ),
            **self._summary_list(request, lists),
        )

    @staticmethod
    def _summary_list(request, lists):
        summary = request.fixed.get("summary", "")
        for position, tag in enumerate(SUMMARY_LISTS):
            if summary[position : position + 1] == "Y":
                start = _int(request.get("BP"), 1)
                end = _int(request.get("BQ"), len(lists[position]))
                return {tag: [str(item) for item in lists[position][max(start, 1) - 1 : end]]}
        return {}

    # ---------------------------------------------------------------- items

    async def item_information(self, request, session):
        """17 Item Information -> 18."""
        barcode = normalize_barcode(request.get("AB"))
        fixed = {
            "security_marker": enums.SecurityMarker.OTHER,  # RFID tags, not tattle tape
            "fee_type": enums.FeeType.OTHER,
            "transaction_date": sip2_now(),
        }
        try:
            item = await self.ils.get_item(barcode)
        except ILSError as exc:
            message = self._msg(self._failure("item_information", exc, item=barcode))
            item = None
        else:
            message = None
            if item is None:
                logger.info(
                    "unknown item",
                    extra={"event": "item_information", "outcome": "unknown_item", "item": barcode},
                )
                message = self._msg("unknown_item")
        if item is None:
            return build(
                "18",
                {**fixed, "circulation_status": enums.CirculationStatus.OTHER},
                AB=barcode,
                AJ="",
                AF=message,
            )

        status = item.get("status")
        if status not in enums.ITEM_STATUS:
            logger.warning(
                "unknown ILS item status",
                extra={"event": "item_information", "status": status, "item": barcode},
            )
        hold_queue = item.get("hold_queue_length")
        return build(
            "18",
            {
                **fixed,
                "circulation_status": enums.ITEM_STATUS.get(status, enums.CirculationStatus.OTHER),
            },
            AB=barcode,
            AJ=item.get("title", ""),
            CK=enums.MediaType.BOOK,
            **compact(
                AH=to_sip2_date(item.get("due_date")),
                AQ=item.get("location"),
                AP=item.get("current_location") or item.get("location"),
                CF=str(hold_queue) if hold_queue is not None else None,
                BG=item.get("owner"),
                AF=item.get("messages") or None,
            ),
        )

    # ---------------------------------------------------------- circulation

    async def _circulate(self, event, call, refuse, **context):
        """Run a circulation call; on failure return ``refuse(title, message, ambiguous)``."""
        try:
            return await call(), None
        except ILSNotFound:
            logger.info(
                "%s refused: unknown item",
                event,
                extra={"event": event, "outcome": "unknown_item", **context},
            )
            return None, refuse("", self._msg("unknown_item"), False)
        except ILSRejected as exc:
            logger.info(
                "%s refused",
                event,
                extra={"event": event, "outcome": "refused", "code": exc.code, **context},
            )
            return None, refuse(exc.title, exc.user_message, False)
        except ILSError as exc:
            key = self._failure(event, exc, **context)
            return None, refuse("", self._msg(key), key == "ambiguous")

    async def checkout(self, request, session):
        """11 Checkout -> 12."""
        item = normalize_barcode(request.get("AB"))
        patron = normalize_barcode(request.get("AA"))
        result, refusal = await self._circulate(
            "checkout",
            lambda: self.ils.checkout(
                item_barcode=item,
                patron_barcode=patron,
                user_pid=session.user_pid,
                terminal=session.terminal,
                transaction_id=request.transaction_id,
            ),
            lambda title, message, ambiguous: self._refuse_12(
                request, session, message, title, ambiguous
            ),
            item=item,
            patron=pseudonymise(patron),
            terminal=session.terminal,
            transaction_id=request.transaction_id,
        )
        if refusal:
            return refusal
        logger.info(
            "checkout ok",
            extra={
                "event": "checkout",
                "outcome": "ok",
                "item": item,
                "patron": pseudonymise(patron),
                "terminal": session.terminal,
                "transaction_id": request.transaction_id,
                "due": result.get("due_date"),
            },
        )
        return build(
            "12",
            {
                "ok": "1",
                "renewal_ok": _yn(result.get("is_renewal")),
                "magnetic_media": "N",
                "desensitize": "Y",
                "transaction_date": sip2_now(),
            },
            AO=self._institution(request, session),
            AA=patron,
            AB=item,
            AJ=result.get("title", ""),
            AH=to_sip2_date(result.get("due_date")),
            CK=enums.MediaType.BOOK,
            AF=result.get("messages") or None,
        )

    async def checkin(self, request, session):
        """09 Checkin -> 10."""
        item = normalize_barcode(request.get("AB"))
        result, refusal = await self._circulate(
            "checkin",
            lambda: self.ils.checkin(
                item_barcode=item,
                user_pid=session.user_pid,
                terminal=session.terminal,
                transaction_id=request.transaction_id,
            ),
            lambda title, message, ambiguous: self._refuse_10(
                request, session, message, title, ambiguous
            ),
            item=item,
            terminal=session.terminal,
            transaction_id=request.transaction_id,
        )
        if refusal:
            return refusal
        logger.info(
            "checkin ok",
            extra={
                "event": "checkin",
                "outcome": "ok",
                "item": item,
                "terminal": session.terminal,
                "transaction_id": request.transaction_id,
            },
        )
        return build(
            "10",
            # alert=Y makes the device beep: e.g. the item has a pending hold
            # and should go to the desk rather than the shelf.
            {
                "ok": "1",
                "resensitize": "Y",
                "magnetic_media": "N",
                "alert": _yn(result.get("alert")),
                "transaction_date": sip2_now(),
            },
            AO=self._institution(request, session),
            AB=item,
            AQ=result.get("permanent_location", ""),
            AJ=result.get("title"),
            CK=enums.MediaType.BOOK,
            AF=result.get("messages") or None,
        )

    async def renew(self, request, session):
        """29 Renew -> 30."""
        item = normalize_barcode(request.get("AB"))
        patron = normalize_barcode(request.get("AA"))
        result, refusal = await self._circulate(
            "renew",
            lambda: self.ils.renew(
                item_barcode=item,
                patron_barcode=patron,
                user_pid=session.user_pid,
                terminal=session.terminal,
                transaction_id=request.transaction_id,
            ),
            lambda title, message, ambiguous: self._refuse_30(
                request, session, message, title, ambiguous
            ),
            item=item,
            patron=pseudonymise(patron),
            terminal=session.terminal,
            transaction_id=request.transaction_id,
        )
        if refusal:
            return refusal
        logger.info(
            "renew ok",
            extra={
                "event": "renew",
                "outcome": "ok",
                "item": item,
                "patron": pseudonymise(patron),
                "terminal": session.terminal,
                "transaction_id": request.transaction_id,
                "due": result.get("due_date"),
            },
        )
        return build(
            "30",
            {
                "ok": "1",
                "renewal_ok": "Y",
                "magnetic_media": "N",
                "desensitize": "Y",
                "transaction_date": sip2_now(),
            },
            AO=self._institution(request, session),
            AA=patron,
            AB=item,
            AJ=result.get("title", ""),
            AH=to_sip2_date(result.get("due_date")),
            CK=enums.MediaType.BOOK,
            AF=result.get("messages") or None,
        )

    async def end_patron_session(self, request, session):
        """35 End Patron Session -> 36. No patron state is kept, so nothing to clear."""
        return build(
            "36",
            {"end_session": "Y", "transaction_date": sip2_now()},
            AO=self._institution(request, session),
            AA=normalize_barcode(request.get("AA")),
        )

    # ------------------------------------------------------------ refusals

    def refuse(self, request, session, message_key):
        """A well-formed negative reply to any known request code."""
        builder = getattr(self, f"_refuse_{REPLY_CODE[request.code]}")
        return builder(request, session, self._msg(message_key))

    def _base(self, request, session):
        return {
            "AO": self._institution(request, session),
            "AA": normalize_barcode(request.get("AA")),
        }

    def _refuse_94(self, request, session, message):
        return build("94", {"ok": "0"})

    def _refuse_98(self, request, session, message):
        return self._acs_status(session, online=False)

    def _refuse_patron(self, code, request, session, message):
        return build(
            code,
            {
                "patron_status": DENIED_STATUS,
                "language": self._language(session),
                "transaction_date": sip2_now(),
            },
            **self._base(request, session),
            AE="",
            BL="N",
            AF=message,
        )

    def _refuse_24(self, request, session, message):
        return self._refuse_patron("24", request, session, message)

    def _refuse_26(self, request, session, message):
        return self._refuse_patron("26", request, session, message)

    def _refuse_64(self, request, session, message):
        return build(
            "64",
            {
                "patron_status": DENIED_STATUS,
                "language": self._language(session),
                "transaction_date": sip2_now(),
                **{f"{name}_count": "0000" for name in _COUNT_NAMES},
            },
            **self._base(request, session),
            AE="",
            BL="N",
            AF=message,
        )

    def _refuse_18(self, request, session, message):
        return build(
            "18",
            {
                "circulation_status": enums.CirculationStatus.OTHER,
                "security_marker": enums.SecurityMarker.OTHER,
                "fee_type": enums.FeeType.OTHER,
                "transaction_date": sip2_now(),
            },
            AB=normalize_barcode(request.get("AB")),
            AJ="",
            AF=message,
        )

    def _refuse_12(self, request, session, message, title="", ambiguous=False):
        return build(
            "12",
            {
                "ok": "0",
                "renewal_ok": "N",
                "magnetic_media": "N",
                "desensitize": "N",
                "transaction_date": sip2_now(),
            },
            ambiguous=ambiguous,
            **self._base(request, session),
            AB=normalize_barcode(request.get("AB")),
            AJ=title or "",
            AH="",
            AF=message,
        )

    def _refuse_10(self, request, session, message, title="", ambiguous=False):
        return build(
            "10",
            {
                "ok": "0",
                "resensitize": "N",
                "magnetic_media": "N",
                "alert": "Y",
                "transaction_date": sip2_now(),
            },
            ambiguous=ambiguous,
            AO=self._institution(request, session),
            AB=normalize_barcode(request.get("AB")),
            AQ="",
            AJ=title or "",
            AF=message,
        )

    def _refuse_30(self, request, session, message, title="", ambiguous=False):
        return build(
            "30",
            {
                "ok": "0",
                "renewal_ok": "N",
                "magnetic_media": "N",
                "desensitize": "N",
                "transaction_date": sip2_now(),
            },
            ambiguous=ambiguous,
            **self._base(request, session),
            AB=normalize_barcode(request.get("AB")),
            AJ=title or "",
            AH="",
            AF=message,
        )

    def _refuse_36(self, request, session, message):
        return build(
            "36",
            {"end_session": "N", "transaction_date": sip2_now()},
            **self._base(request, session),
            AF=message,
        )

    def _refuse_16(self, request, session, message):
        return build(
            "16",
            {"ok": "0", "available": "N", "transaction_date": sip2_now()},
            **self._base(request, session),
            AF=message,
        )

    def _refuse_20(self, request, session, message):
        return build(
            "20",
            {"item_properties_ok": "0", "transaction_date": sip2_now()},
            AB=normalize_barcode(request.get("AB")),
            AF=message,
        )

    def _refuse_38(self, request, session, message):
        return build(
            "38",
            {"payment_accepted": "N", "transaction_date": sip2_now()},
            **self._base(request, session),
            AF=message,
        )

    def _refuse_66(self, request, session, message):
        return build(
            "66",
            {
                "ok": "0",
                "renewed_count": "0000",
                "unrenewed_count": "0000",
                "transaction_date": sip2_now(),
            },
            AO=self._institution(request, session),
            AF=message,
        )


_COUNT_NAMES = (
    "hold_items",
    "overdue_items",
    "charged_items",
    "fine_items",
    "recall_items",
    "unavailable_holds",
)
_LIST_KEYS = ("holds", "overdue", "loans", "fines", "recalls", "unavailable_holds")


def _int(value, default):
    try:
        return int(value)
    except TypeError, ValueError:
        return default
