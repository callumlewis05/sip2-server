"""SIP2 2.00 code tables, and the ILS vocabulary that maps onto them."""

from enum import StrEnum


class CirculationStatus(StrEnum):
    OTHER = "01"
    ON_ORDER = "02"
    AVAILABLE = "03"
    CHARGED = "04"
    CHARGED_NOT_TO_BE_RECALLED = "05"
    IN_PROCESS = "06"
    RECALLED = "07"
    WAITING_ON_HOLD_SHELF = "08"
    WAITING_TO_BE_RESHELVED = "09"
    IN_TRANSIT = "10"
    CLAIMED_RETURNED = "11"
    LOST = "12"
    MISSING = "13"


class SecurityMarker(StrEnum):
    OTHER = "00"
    NONE = "01"
    TATTLE_TAPE = "02"
    WHISPER_TAPE = "03"


class FeeType(StrEnum):
    OTHER = "01"
    ADMINISTRATIVE = "02"
    DAMAGE = "03"
    OVERDUE = "04"
    PROCESSING = "05"
    RENTAL = "06"
    REPLACEMENT = "07"
    COMPUTER_ACCESS = "08"
    HOLD_FEE = "09"


class MediaType(StrEnum):
    OTHER = "000"
    BOOK = "001"
    MAGAZINE = "002"
    BOUND_JOURNAL = "003"
    AUDIO_TAPE = "004"
    VIDEO_TAPE = "005"
    CD_OR_CDROM = "006"
    DISKETTE = "007"
    BOOK_WITH_DISKETTE = "008"
    BOOK_WITH_CD = "009"
    BOOK_WITH_AUDIO_TAPE = "010"


PATRON_STATUS_FLAGS = (
    "charge_privileges_denied",
    "renewal_privileges_denied",
    "recall_privileges_denied",
    "hold_privileges_denied",
    "card_reported_lost",
    "too_many_items_charged",
    "too_many_items_overdue",
    "too_many_renewals",
    "too_many_claims_of_items_returned",
    "too_many_items_lost",
    "excessive_outstanding_fines",
    "excessive_outstanding_fees",
    "recall_overdue",
    "too_many_items_billed",
)
"""ILS ``status_flags`` vocabulary, in the order of the 14-char patron status
field. The names match invenio-sip2's ``PatronStatusTypes`` values, so the
ILS contract is unchanged."""

DENIED_FLAGS = (
    "charge_privileges_denied",
    "renewal_privileges_denied",
    "hold_privileges_denied",
)
"""Flags set on a patron we cannot vouch for (unknown, or ILS down)."""


def patron_status(flags):
    """``(14-char field, [unknown flags])`` for a list of ILS status flags."""
    flags = set(flags or ())
    unknown = sorted(str(flag) for flag in flags - set(PATRON_STATUS_FLAGS))
    field = "".join("Y" if name in flags else " " for name in PATRON_STATUS_FLAGS)
    return field, unknown


ITEM_STATUS = {
    "available": CirculationStatus.AVAILABLE,
    "on_loan": CirculationStatus.CHARGED,
    "recalled": CirculationStatus.RECALLED,
    "on_hold_shelf": CirculationStatus.WAITING_ON_HOLD_SHELF,
    "in_transit": CirculationStatus.IN_TRANSIT,
    "in_process": CirculationStatus.IN_PROCESS,
    "on_order": CirculationStatus.ON_ORDER,
    "lost": CirculationStatus.LOST,
    "missing": CirculationStatus.MISSING,
    "other": CirculationStatus.OTHER,
}
"""ILS item status vocabulary (see ILSClient.get_item) -> SIP2 circulation status."""

LANGUAGES = {
    "en": "001",
    "fr": "002",
    "de": "003",
    "it": "004",
    "nl": "005",
    "sv": "006",
    "fi": "007",
    "es": "008",
    "da": "009",
    "pt": "010",
    "no": "012",
    "he": "013",
    "ja": "014",
    "ru": "015",
    "ar": "016",
    "pl": "017",
    "el": "018",
    "zh": "019",
    "ko": "020",
}
"""ISO 639-1 -> SIP2 3-digit language code. ``000`` is "unknown"."""


def language_code(language):
    return LANGUAGES.get((language or "").lower()[:2], "000")
