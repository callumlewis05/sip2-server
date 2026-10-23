"""SIP2 callback handlers.

invenio-sip2 calls these from ``invenio_sip2/actions/actions.py``.  The
positional parameters below mirror exactly what the library passes (1.x.x);
every handler also takes ``**kwargs`` because the library mixes positional
and keyword arguments and in one place misspells ``institution_id``.

Design rules:

* Handlers never contain circulation policy. They validate nothing beyond
  "did the ILS say yes"; the ILS decides.
* ILS *rejections* (policy) and ILS *unavailability* (outage) are both turned
  into a well-formed negative SIP2 response, with different screen messages
  and different log levels.
* All model objects are built with keyword arguments so small signature
  changes in invenio-sip2 do not reorder our values.
"""

import logging
import uuid

from flask import current_app
from invenio_sip2.errors import SelfcheckCirculationError
from invenio_sip2.models import (
    PatronStatus,
    PatronStatusTypes,
    SelfcheckCheckin,
    SelfcheckCheckout,
    SelfcheckCirculationStatus,
    SelfcheckEnablePatron,
    SelfcheckFeeType,
    SelfcheckItemInformation,
    SelfcheckMediaType,
    SelfcheckPatronInformation,
    SelfcheckPatronStatus,
    SelfcheckRenew,
    SelfcheckSecurityMarkerType,
)

from .ext import current_cds_sip2
from .ils_client import ILSError, ILSRejected
from .utils import compact, normalize_barcode, to_sip2_date

logger = logging.getLogger("cds_sip2.handlers")

#: ILS item status vocabulary (see ILSClient.get_item) -> SIP2 circulation status.
#: Attribute access is deliberate: a renamed enum member fails at import time,
#: in CI, rather than silently at a kiosk.
ITEM_STATUS_MAP = {
    "available": SelfcheckCirculationStatus.AVAILABLE,
    "on_loan": SelfcheckCirculationStatus.CHARGED,
    "recalled": SelfcheckCirculationStatus.RECALLED,
    "on_hold_shelf": SelfcheckCirculationStatus.WAITING_ON_HOLD_SHELF,
    "in_transit": SelfcheckCirculationStatus.IN_TRANSIT,
    "in_process": SelfcheckCirculationStatus.IN_PROCESS,
    "on_order": SelfcheckCirculationStatus.ON_ORDER,
    "lost": SelfcheckCirculationStatus.LOST,
    "missing": SelfcheckCirculationStatus.MISSING,
}

#: Flags set on a patron we cannot vouch for (unknown, or ILS down).
DENIED_FLAGS = (
    PatronStatusTypes.CHARGE_PRIVILEGES_DENIED,
    PatronStatusTypes.RENEWAL_PRIVILEGES_DENIED,
    PatronStatusTypes.HOLD_PRIVILEGES_DENIED,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ils():
    return current_cds_sip2.ils


def _cfg(key, default=None):
    return current_app.config.get(key, default)


def _msg(key):
    return _cfg("CDS_SIP2_MESSAGES", {}).get(key, "Please ask at the library desk.")


def _language(language=None):
    return language or _cfg("CDS_SIP2_LANGUAGE", "en")


def _txid():
    return uuid.uuid4().hex


def _patron_status(flags):
    """Build a ``PatronStatus`` from the ILS's list of ``PatronStatusTypes`` values."""
    status = PatronStatus()
    for flag in flags or ():
        try:
            status.add_patron_status_type(PatronStatusTypes(flag))
        except ValueError:
            logger.warning("ILS sent unknown patron status flag %r; ignoring", flag)
    return status


def _denied_status():
    status = PatronStatus()
    for flag in DENIED_FLAGS:
        status.add_patron_status_type(flag)
    return status


def _circulation_error(message, data):
    """Build a ``SelfcheckCirculationError`` carrying ``data`` as the response.

    The actions only ever read ``error.data``; tolerate both constructor
    shapes so a minor invenio-sip2 change does not turn a refused checkout
    into a crashed session.
    """
    try:
        return SelfcheckCirculationError(message, data=data)
    except TypeError:
        error = SelfcheckCirculationError(message)
        error.data = data
        return error


def _patron_lists(patron):
    """Pick the list fields of a patron payload; each becomes repeated SIP2 fields."""
    return compact(
        charged_items=patron.get("loans"),
        overdue_items=patron.get("overdue"),
        hold_items=patron.get("holds"),
        recall_items=patron.get("recalls"),
        unavailable_items=patron.get("unavailable_holds"),
        fine_items=patron.get("fines"),
    )


# ---------------------------------------------------------------------------
# login / status
# ---------------------------------------------------------------------------


def selfcheck_login(login, password, terminal_ip=None, **kwargs):
    """93 Login -> 94.

    Return a dict to authenticate the device; it is merged into the client
    session in Redis, and ``transaction_user_id`` / ``institution_id`` /
    ``terminal`` are handed back to the circulation handlers later.  Return
    ``None`` to refuse.
    """
    try:
        result = _ils().login(login, password, ip=terminal_ip)
    except ILSError as exc:
        logger.error("login: ILS error for terminal %r from %s: %s", login, terminal_ip, exc)
        return None
    if not result:
        logger.warning("login: refused for terminal %r from %s", login, terminal_ip)
        return None
    if result.get("user_pid") in (None, ""):
        logger.error("login: ILS accepted terminal %r but sent no user_pid; refusing", login)
        return None

    logger.info("login: terminal %r authenticated from %s", login, terminal_ip)
    return {
        "authenticated": True,
        "terminal": login,
        "transaction_user_id": str(result["user_pid"]),
        "institution_id": result.get("institution_id") or _cfg("CDS_SIP2_INSTITUTION_ID"),
        "library_name": result.get("library_name") or _cfg("CDS_SIP2_LIBRARY_NAME"),
        "library_language": result.get("language") or _language(),
        # Kept in the session for operators reading Redis; the ILS resolves the
        # transaction location from ``terminal`` on every call.
        "location_pid": result.get("location_pid"),
    }


def system_status(terminal, institution_id=None, **kwargs):
    """99 SC Status -> 98 ACS Status.

    The capability flags in the 98 come from ``SIP2_SUPPORT_*`` config; what
    we return here is only stored on the client session.
    """
    online = _ils().ping()
    if not online:
        logger.warning("system_status: ILS not reachable (terminal %r)", terminal)
    return {"institution_id": institution_id, "terminal": terminal, "online": online}


# ---------------------------------------------------------------------------
# patrons
# ---------------------------------------------------------------------------


def validate_patron(patron_id, institution_id=None, **kwargs):
    """Does the patron exist? (BL field)."""
    barcode = normalize_barcode(patron_id)
    try:
        return _ils().get_patron(barcode) is not None
    except ILSError as exc:
        logger.error("validate_patron: ILS error for %r: %s", barcode, exc)
        return False


def authorize_patron(patron_id, password, institution_id=None, **kwargs):
    """Is the PIN (AD field) correct? (CQ field)."""
    barcode = normalize_barcode(patron_id)
    try:
        return _ils().verify_patron_pin(barcode, password)
    except ILSError as exc:
        logger.error("authorize_patron: ILS error for %r: %s", barcode, exc)
        return False


def patron_status(patron_id, institution_id=None, **kwargs):
    """23 Patron Status Request -> 24."""
    barcode = normalize_barcode(patron_id)
    language = _language()
    try:
        patron = _ils().get_patron(barcode)
    except ILSError as exc:
        logger.error("patron_status: ILS error for %r: %s", barcode, exc)
        patron = None
        message = _msg("unavailable")
    else:
        message = _msg("unknown_patron")

    if patron is None:
        return SelfcheckPatronStatus(
            patron_status=_denied_status(),
            language=language,
            patron_name="",
            patron_id=barcode,
            institution_id=institution_id,
            valid_patron="N",
            screen_messages=[message],
        )
    return SelfcheckPatronStatus(
        patron_status=_patron_status(patron.get("status_flags")),
        language=language,
        patron_name=patron.get("name", ""),
        patron_id=barcode,
        institution_id=institution_id,
        valid_patron="Y",
    )


def enable_patron(patron_id, institution_id=None, **kwargs):
    """25 Patron Enable -> 26.

    Kiosks cannot lift blocks in CDS-ILS; we simply report the current status.
    """
    barcode = normalize_barcode(patron_id)
    try:
        patron = _ils().get_patron(barcode)
    except ILSError as exc:
        logger.error("enable_patron: ILS error for %r: %s", barcode, exc)
        patron = None
    return SelfcheckEnablePatron(
        patron_id=barcode,
        institution_id=institution_id,
        patron_name=(patron or {}).get("name", ""),
        patron_status=_patron_status(patron.get("status_flags")) if patron else _denied_status(),
        language=_language(),
    )


def patron_information(patron_id, institution_id=None, **kwargs):
    """63 Patron Information -> 64.

    The ``*_items`` lists drive the six 4-digit counts in the fixed part of
    the 64 and are repeated as AS/AT/AU/AV/BU/CD fields when the device asked
    for them in its summary.
    """
    barcode = normalize_barcode(patron_id)
    language = _language()
    try:
        patron = _ils().get_patron(barcode)
    except ILSError as exc:
        logger.error("patron_information: ILS error for %r: %s", barcode, exc)
        return SelfcheckPatronInformation(
            patron_id=barcode,
            patron_name="",
            patron_status=_denied_status(),
            institution_id=institution_id,
            language=language,
            valid_patron="N",
            screen_messages=[_msg("unavailable")],
        )
    if patron is None:
        logger.info("patron_information: unknown patron %r", barcode)
        return SelfcheckPatronInformation(
            patron_id=barcode,
            patron_name="",
            patron_status=_denied_status(),
            institution_id=institution_id,
            language=language,
            valid_patron="N",
            screen_messages=[_msg("unknown_patron")],
        )

    fines_total = patron.get("fines_total")
    has_fines = bool(fines_total) and str(fines_total) not in ("0", "0.0", "0.00")
    return SelfcheckPatronInformation(
        patron_id=barcode,
        patron_name=patron.get("name", ""),
        patron_status=_patron_status(patron.get("status_flags")),
        institution_id=institution_id,
        language=language,
        valid_patron="Y",
        **_patron_lists(patron),
        **compact(
            email=patron.get("email"),
            home_address=patron.get("address"),
            home_phone=patron.get("phone"),
            fee_amount=str(fines_total) if has_fines else None,
            currency_type=patron.get("currency") if has_fines else None,
            screen_messages=patron.get("messages") or None,
        ),
    )


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


def item_information(item_id, terminal=None, language=None, institution_id=None, **kwargs):
    """17 Item Information -> 18."""
    barcode = normalize_barcode(item_id)

    def _unknown(message):
        return SelfcheckItemInformation(
            item_id=barcode,
            title_id="",
            circulation_status=SelfcheckCirculationStatus.OTHER,
            fee_type=SelfcheckFeeType.OTHER,
            security_marker=SelfcheckSecurityMarkerType.OTHER,
            screen_messages=[message],
        )

    try:
        item = _ils().get_item(barcode)
    except ILSError as exc:
        logger.error("item_information: ILS error for %r: %s", barcode, exc)
        return _unknown(_msg("unavailable"))
    if item is None:
        logger.info("item_information: unknown item %r", barcode)
        return _unknown(_msg("unknown_item"))

    status = item.get("status")
    if status not in ITEM_STATUS_MAP and status != "other":
        logger.warning("item_information: unknown ILS item status %r for %r", status, barcode)
    hold_queue = item.get("hold_queue_length")
    return SelfcheckItemInformation(
        item_id=barcode,
        title_id=item.get("title", ""),
        circulation_status=ITEM_STATUS_MAP.get(status, SelfcheckCirculationStatus.OTHER),
        fee_type=SelfcheckFeeType.OTHER,
        # RFID tags, not tattle tape.
        security_marker=SelfcheckSecurityMarkerType.OTHER,
        **compact(
            media_type=SelfcheckMediaType.BOOK,
            due_date=to_sip2_date(item.get("due_date")),
            permanent_location=item.get("location"),
            current_location=item.get("current_location") or item.get("location"),
            hold_queue_length=str(hold_queue) if hold_queue is not None else None,
            owner=item.get("owner"),
            screen_messages=item.get("messages") or None,
        ),
    )


# ---------------------------------------------------------------------------
# circulation
# ---------------------------------------------------------------------------


def checkout(transaction_user_id, item_id, patron_id, institution_id=None, terminal=None,
             language=None, **kwargs):
    """11 Checkout -> 12.

    Failures are *raised* as ``SelfcheckCirculationError`` whose ``.data`` is
    the negative response; the Checkout action catches it and still answers
    the device with a well-formed 12 (ok=0).
    """
    item = normalize_barcode(item_id)
    patron = normalize_barcode(patron_id)

    def _failure(title, message):
        return SelfcheckCheckout(
            title_id=title or "",
            checkout=False,
            renewal=False,
            magnetic_media=False,
            desensitize=False,
            item_id=item,
            patron_id=patron,
            screen_messages=[message],
        )

    try:
        result = _ils().checkout(
            item_barcode=item,
            patron_barcode=patron,
            user_pid=transaction_user_id,
            terminal=terminal,
            transaction_id=_txid(),
        )
    except ILSRejected as exc:
        logger.info("checkout refused: item=%r patron=%r terminal=%r code=%s",
                    item, patron, terminal, exc.code)
        raise _circulation_error(str(exc), _failure(exc.title, exc.user_message))
    except ILSError as exc:
        logger.error("checkout failed: item=%r patron=%r terminal=%r: %s",
                     item, patron, terminal, exc)
        raise _circulation_error(str(exc), _failure("", _msg("unavailable")))

    logger.info("checkout ok: item=%r patron=%r terminal=%r due=%s",
                item, patron, terminal, result.get("due_date"))
    return SelfcheckCheckout(
        title_id=result.get("title", ""),
        checkout=True,
        renewal=bool(result.get("is_renewal")),
        magnetic_media=False,
        desensitize=True,
        item_id=item,
        patron_id=patron,
        **compact(
            due_date=to_sip2_date(result.get("due_date")),
            media_type=SelfcheckMediaType.BOOK,
            screen_messages=result.get("messages") or None,
        ),
    )


def checkin(transaction_user_id, item_id, institution_id=None, terminal=None, language=None,
            **kwargs):
    """09 Checkin -> 10."""
    item = normalize_barcode(item_id)

    def _failure(title, message):
        return SelfcheckCheckin(
            permanent_location="",
            checkin=False,
            alert=True,
            magnetic_media=False,
            resensitize=False,
            item_id=item,
            title_id=title or "",
            screen_messages=[message],
        )

    try:
        result = _ils().checkin(
            item_barcode=item,
            user_pid=transaction_user_id,
            terminal=terminal,
            transaction_id=_txid(),
        )
    except ILSRejected as exc:
        logger.info("checkin refused: item=%r terminal=%r code=%s", item, terminal, exc.code)
        raise _circulation_error(str(exc), _failure(exc.title, exc.user_message))
    except ILSError as exc:
        logger.error("checkin failed: item=%r terminal=%r: %s", item, terminal, exc)
        raise _circulation_error(str(exc), _failure("", _msg("unavailable")))

    logger.info("checkin ok: item=%r terminal=%r", item, terminal)
    return SelfcheckCheckin(
        permanent_location=result.get("permanent_location", ""),
        checkin=True,
        # alert=True makes the device beep: e.g. the item has a pending hold
        # and should go to the desk rather than the shelf.
        alert=bool(result.get("alert")),
        magnetic_media=False,
        resensitize=True,
        item_id=item,
        **compact(
            title_id=result.get("title"),
            media_type=SelfcheckMediaType.BOOK,
            screen_messages=result.get("messages") or None,
        ),
    )


def renew(transaction_user_id, item_id, patron_id=None, institution_id=None, terminal=None,
          language=None, **kwargs):
    """29 Renew -> 30.

    ``institution_id`` arrives misspelled (``intitution_id``) from the
    library; it lands in ``kwargs`` and we do not need it.
    """
    item = normalize_barcode(item_id)
    patron = normalize_barcode(patron_id)

    def _failure(title, message):
        return SelfcheckRenew(
            title_id=title or "",
            success=False,
            renew=False,
            renewal=False,
            magnetic_media=False,
            desensitize=False,
            item_id=item,
            patron_id=patron,
            screen_messages=[message],
        )

    try:
        result = _ils().renew(
            item_barcode=item,
            patron_barcode=patron,
            user_pid=transaction_user_id,
            terminal=terminal,
            transaction_id=_txid(),
        )
    except ILSRejected as exc:
        logger.info("renew refused: item=%r patron=%r terminal=%r code=%s",
                    item, patron, terminal, exc.code)
        raise _circulation_error(str(exc), _failure(exc.title, exc.user_message))
    except ILSError as exc:
        logger.error("renew failed: item=%r patron=%r terminal=%r: %s",
                     item, patron, terminal, exc)
        raise _circulation_error(str(exc), _failure("", _msg("unavailable")))

    logger.info("renew ok: item=%r patron=%r terminal=%r due=%s",
                item, patron, terminal, result.get("due_date"))
    return SelfcheckRenew(
        title_id=result.get("title", ""),
        success=True,
        renew=True,
        renewal=True,
        magnetic_media=False,
        desensitize=True,
        item_id=item,
        patron_id=patron,
        **compact(
            due_date=to_sip2_date(result.get("due_date")),
            media_type=SelfcheckMediaType.BOOK,
            screen_messages=result.get("messages") or None,
        ),
    )
