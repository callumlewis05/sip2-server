"""The seam between the ``sip2`` and ``ils`` packages.

The SIP2 side depends only on this Protocol, never on HTTP. Payloads are the
plain dicts documented on :class:`~cds_sip2.ils.client.ILSClient`; failures
are the exceptions in :mod:`cds_sip2.ils.errors`.

Circulation calls take ``terminal`` and ``user_pid`` explicitly, from the
SIP2 session established at login, and a ``transaction_id`` derived from the
request content so the ILS can recognise a resend.
"""

from typing import Protocol


class ILSBackend(Protocol):
    async def ping(self) -> bool:
        """True if the ILS answers. Never raises."""

    async def login(self, terminal: str, password: str, ip: str | None = None) -> dict | None:
        """Terminal credentials -> ``{"user_pid", ...}``, or ``None`` if refused."""

    async def get_patron(self, barcode: str) -> dict | None:
        """Patron payload, or ``None`` if unknown."""

    async def verify_patron_pin(self, barcode: str, pin: str) -> bool:
        """True if the PIN is correct. False for an unknown patron."""

    async def get_item(self, barcode: str) -> dict | None:
        """Item payload, or ``None`` if unknown."""

    async def checkout(
        self,
        *,
        item_barcode: str,
        patron_barcode: str,
        user_pid: str,
        terminal: str,
        transaction_id: str,
    ) -> dict: ...

    async def checkin(
        self,
        *,
        item_barcode: str,
        user_pid: str,
        terminal: str,
        transaction_id: str,
    ) -> dict: ...

    async def renew(
        self,
        *,
        item_barcode: str,
        patron_barcode: str,
        user_pid: str,
        terminal: str,
        transaction_id: str,
    ) -> dict: ...

    async def aclose(self) -> None: ...
