"""Flask extension for the CDS SIP2 sidecar.

Registered under ``invenio_base.apps``. Its only job is to build one shared
:class:`~cds_sip2.ils_client.ILSClient` and expose it to the handlers through
:data:`current_cds_sip2`.
"""

from flask import current_app
from werkzeug.local import LocalProxy

from . import config
from .ils_client import ILSClient


class CDSSip2:
    """CDS SIP2 sidecar extension."""

    def __init__(self, app=None):
        self.ils = None
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        self.init_config(app)
        self.ils = ILSClient(
            base_url=app.config["CDS_SIP2_ILS_URL"],
            token=app.config["CDS_SIP2_ILS_TOKEN"],
            timeout=(
                app.config["CDS_SIP2_ILS_CONNECT_TIMEOUT"],
                app.config["CDS_SIP2_ILS_READ_TIMEOUT"],
            ),
        )
        if not app.config["CDS_SIP2_ILS_TOKEN"]:
            app.logger.warning("CDS_SIP2_ILS_TOKEN is empty: every ILS call will be rejected")
        app.extensions["cds-sip2"] = self

    @staticmethod
    def init_config(app):
        """Apply our defaults without overriding values already present.

        In production the values are already there thanks to the
        ``invenio_config.module`` entry point; this makes the extension also
        usable in a bare ``Flask()`` app, as in the tests.
        """
        for key in dir(config):
            if key.startswith(("CDS_SIP2_", "SIP2_", "BABEL_")):
                app.config.setdefault(key, getattr(config, key))


current_cds_sip2 = LocalProxy(lambda: current_app.extensions["cds-sip2"])
"""Proxy to the current :class:`CDSSip2` extension (needs an app context)."""
