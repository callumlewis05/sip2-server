"""SIP2 server for CDS-ILS.

An asyncio SIP2 server that translates every SIP2 request into a call
against the ``/api/sip2`` REST endpoints exposed by the ILS. Run it with
``python -m cds_sip2``.

Two packages with a strict boundary: :mod:`cds_sip2.sip2` speaks SIP2,
:mod:`cds_sip2.rest` speaks HTTP. Both depend on :mod:`cds_sip2.domain` (the
typed values and the ``ILSBackend`` port) and meet only in ``__main__``.
"""

__version__ = "0.2.0"

__all__ = ("__version__",)
