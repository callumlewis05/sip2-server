"""REST adapter: implements :class:`cds_sip2.domain.ILSBackend` over the
CDS-ILS ``/api/sip2`` endpoints. Knows nothing about SIP2."""

from .client import ILSClient

__all__ = ("ILSClient",)
