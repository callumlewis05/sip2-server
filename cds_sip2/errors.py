"""Errors raised by an :class:`~cds_sip2.domain.ILSBackend`.

=====================  ==========================================================
Exception              Meaning
=====================  ==========================================================
``ILSNotFound``        404 on a write: the ILS does not know the item.
``ILSRejected``        The ILS declined (409 / other 4xx, or ``{"ok": false}``).
``ILSUnavailable``     Nothing was processed: connect failure, or 5xx / timeout on
                       a lookup.
``ILSAmbiguous``       A write was sent but no usable answer came back. The
                       ILS may or may not have recorded it.
``ILSConfigError``     The ILS rejected our service token (401/403).
=====================  ==========================================================

Unknown patrons and items on *lookups* are not errors: they return ``None``.
"""


class ILSError(Exception):
    """Base class for all ILS client errors."""


class ILSUnavailable(ILSError):
    """The ILS could not be reached, or failed before processing anything."""


class ILSAmbiguous(ILSError):
    """A circulation request may or may not have been processed.

    Raised only for writes (checkout, checkin, item status update), when the
    request left this process but no usable response arrived (read timeout,
    connection reset, 5xx from a proxy in front of the ILS, or a 2xx body that
    cannot be read). Never raised by lookups or by a connect failure, where
    nothing was sent.
    """

    def __init__(self, message, transaction_id=None):
        self.transaction_id = transaction_id
        super().__init__(message)


class ILSConfigError(ILSError):
    """The ILS rejected the server's own credentials (401/403). Page an operator."""


class ILSNotFound(ILSError):
    """404 on a write: the item is unknown to the ILS."""


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
