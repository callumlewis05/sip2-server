"""SIP2 sidecar for CDS-ILS.

A minimal Invenio application that hosts the invenio-sip2 socket server and
translates every SIP2 request into a call against the ``/api/sip2`` REST
endpoints exposed by the ILS.
"""

from .ext import CDSSip2

__version__ = "0.1.0"

__all__ = ("CDSSip2", "__version__")
