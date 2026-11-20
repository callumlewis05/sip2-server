"""The ILS as the SIP2 side sees it: typed values and the port it calls.

Nothing here knows SIP2 tags or HTTP. :mod:`cds_sip2.rest` parses the REST
payloads into these types once, and :mod:`cds_sip2.sip2` maps them onto SIP2
code tables. The enums are ``StrEnum`` so their values are the wire strings
of the REST contract.

Scope: item lookups and the inventory-side transactions (checkout, checkin,
item status update). :class:`PatronBlock` is defined as the reference
vocabulary for later patron support, but no workflow uses it yet.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is asking, handed to every ILS write.

    ``terminal`` lets the ILS resolve the transaction location, ``user_pid``
    is the ILS user it acts as, and ``transaction_id`` is derived from the
    SIP2 request so the ILS can recognise a resend.
    """

    terminal: str
    user_pid: str
    transaction_id: str


class ItemStatus(StrEnum):
    AVAILABLE = "available"
    ON_LOAN = "on_loan"
    RECALLED = "recalled"
    ON_HOLD_SHELF = "on_hold_shelf"
    IN_TRANSIT = "in_transit"
    IN_PROCESS = "in_process"
    ON_ORDER = "on_order"
    LOST = "lost"
    MISSING = "missing"
    OTHER = "other"


class MediaType(StrEnum):
    BOOK = "book"
    MAGAZINE = "magazine"
    BOUND_JOURNAL = "bound_journal"
    AUDIO = "audio"
    VIDEO = "video"
    CD = "cd"
    DISKETTE = "diskette"
    BOOK_WITH_CD = "book_with_cd"
    BOOK_WITH_DISKETTE = "book_with_diskette"
    BOOK_WITH_AUDIO = "book_with_audio"
    OTHER = "other"


class PatronBlock(StrEnum):
    """Reference only: not used by any workflow yet."""

    CHARGE_PRIVILEGES_DENIED = "charge_privileges_denied"
    RENEWAL_PRIVILEGES_DENIED = "renewal_privileges_denied"
    RECALL_PRIVILEGES_DENIED = "recall_privileges_denied"
    HOLD_PRIVILEGES_DENIED = "hold_privileges_denied"
    CARD_REPORTED_LOST = "card_reported_lost"
    TOO_MANY_ITEMS_CHARGED = "too_many_items_charged"
    TOO_MANY_ITEMS_OVERDUE = "too_many_items_overdue"
    TOO_MANY_RENEWALS = "too_many_renewals"
    TOO_MANY_CLAIMS_OF_ITEMS_RETURNED = "too_many_claims_of_items_returned"
    TOO_MANY_ITEMS_LOST = "too_many_items_lost"
    EXCESSIVE_OUTSTANDING_FINES = "excessive_outstanding_fines"
    EXCESSIVE_OUTSTANDING_FEES = "excessive_outstanding_fees"
    RECALL_OVERDUE = "recall_overdue"
    TOO_MANY_ITEMS_BILLED = "too_many_items_billed"


@dataclass(frozen=True, slots=True)
class Item:
    barcode: str
    title: str
    status: ItemStatus
    location: str | None = None
    current_location: str | None = None
    due_date: date | None = None
    hold_queue_length: int | None = None
    owner: str | None = None
    media_type: MediaType | None = None
    """``None``: the ILS did not say; the SIP2 side uses its configured default."""
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    title: str
    due_date: date | None
    is_renewal: bool = False
    media_type: MediaType | None = None
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckinResult:
    title: str
    permanent_location: str | None = None
    alert: bool = False
    """The item should go to the desk, not the shelf (e.g. it has a hold)."""
    media_type: MediaType | None = None
    messages: tuple[str, ...] = ()


class ILSBackend(Protocol):
    """The port the SIP2 side calls. :class:`cds_sip2.rest.ILSClient` is the
    real adapter; ``tests/conftest.py:FakeILS`` is the in-memory one.

    Lookups return ``None`` for an unknown barcode. Failures are the
    exceptions in :mod:`cds_sip2.errors`. Writes are never retried: they
    carry ``caller.transaction_id`` so the ILS can answer a resend with the
    original result.
    """

    async def ping(self) -> bool:
        """True if the ILS answers. Never raises."""

    async def get_item(self, barcode: str) -> Item | None: ...

    async def checkout(
        self, *, item_barcode: str, patron_barcode: str, caller: Caller
    ) -> CheckoutResult: ...

    async def checkin(self, *, item_barcode: str, caller: Caller) -> CheckinResult: ...

    async def update_item_status(
        self, *, item_barcode: str, properties: str, caller: Caller
    ) -> Item: ...

    async def aclose(self) -> None: ...
