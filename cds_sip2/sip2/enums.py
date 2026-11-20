"""SIP2 2.00 code tables, and the maps from the domain vocabulary onto them.

The ILS vocabulary itself lives in :mod:`cds_sip2.domain`. The maps here are
explicit, so reordering or extending a domain enum can never shift a SIP2
code or a position in the patron status field.
"""

from enum import StrEnum

from ..domain import ItemStatus
from ..domain import MediaType as DomainMediaType
from ..domain import PatronBlock


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
    """Only ``OTHER`` is sent today (the 18's fixed part requires a fee type)."""

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


CIRCULATION_STATUS = {
    ItemStatus.AVAILABLE: CirculationStatus.AVAILABLE,
    ItemStatus.ON_LOAN: CirculationStatus.CHARGED,
    ItemStatus.RECALLED: CirculationStatus.RECALLED,
    ItemStatus.ON_HOLD_SHELF: CirculationStatus.WAITING_ON_HOLD_SHELF,
    ItemStatus.IN_TRANSIT: CirculationStatus.IN_TRANSIT,
    ItemStatus.IN_PROCESS: CirculationStatus.IN_PROCESS,
    ItemStatus.ON_ORDER: CirculationStatus.ON_ORDER,
    ItemStatus.LOST: CirculationStatus.LOST,
    ItemStatus.MISSING: CirculationStatus.MISSING,
    ItemStatus.OTHER: CirculationStatus.OTHER,
}
"""Every :class:`ItemStatus` -> its SIP2 circulation status (tested to be total)."""

MEDIA_TYPE = {
    DomainMediaType.BOOK: MediaType.BOOK,
    DomainMediaType.MAGAZINE: MediaType.MAGAZINE,
    DomainMediaType.BOUND_JOURNAL: MediaType.BOUND_JOURNAL,
    DomainMediaType.AUDIO: MediaType.AUDIO_TAPE,
    DomainMediaType.VIDEO: MediaType.VIDEO_TAPE,
    DomainMediaType.CD: MediaType.CD_OR_CDROM,
    DomainMediaType.DISKETTE: MediaType.DISKETTE,
    DomainMediaType.BOOK_WITH_CD: MediaType.BOOK_WITH_CD,
    DomainMediaType.BOOK_WITH_DISKETTE: MediaType.BOOK_WITH_DISKETTE,
    DomainMediaType.BOOK_WITH_AUDIO: MediaType.BOOK_WITH_AUDIO_TAPE,
    DomainMediaType.OTHER: MediaType.OTHER,
}
"""Every domain media type -> SIP2 ``CK`` code (tested to be total)."""

PATRON_STATUS_POSITIONS = (
    PatronBlock.CHARGE_PRIVILEGES_DENIED,
    PatronBlock.RENEWAL_PRIVILEGES_DENIED,
    PatronBlock.RECALL_PRIVILEGES_DENIED,
    PatronBlock.HOLD_PRIVILEGES_DENIED,
    PatronBlock.CARD_REPORTED_LOST,
    PatronBlock.TOO_MANY_ITEMS_CHARGED,
    PatronBlock.TOO_MANY_ITEMS_OVERDUE,
    PatronBlock.TOO_MANY_RENEWALS,
    PatronBlock.TOO_MANY_CLAIMS_OF_ITEMS_RETURNED,
    PatronBlock.TOO_MANY_ITEMS_LOST,
    PatronBlock.EXCESSIVE_OUTSTANDING_FINES,
    PatronBlock.EXCESSIVE_OUTSTANDING_FEES,
    PatronBlock.RECALL_OVERDUE,
    PatronBlock.TOO_MANY_ITEMS_BILLED,
)
"""Position of each block in the 14-char SIP2 patron status field."""


def patron_status(blocks):
    """14-char patron status field for a set of :class:`PatronBlock`."""
    blocks = set(blocks or ())
    return "".join("Y" if block in blocks else " " for block in PATRON_STATUS_POSITIONS)


DENIED_STATUS = patron_status(
    {
        PatronBlock.CHARGE_PRIVILEGES_DENIED,
        PatronBlock.RENEWAL_PRIVILEGES_DENIED,
        PatronBlock.HOLD_PRIVILEGES_DENIED,
    }
)
"""Patron status sent in every patron-message refusal: we cannot vouch for
the patron, and patron messages are not supported yet."""

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
