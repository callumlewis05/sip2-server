"""Configuration for the CDS SIP2 sidecar.

Everything secret or deployment-specific comes from the environment; the
structural parts (handler wiring, protocol flags) live here.  Any Invenio
config value can additionally be overridden with an ``INVENIO_``-prefixed
environment variable, e.g. ``INVENIO_SIP2_TIMEOUT_PERIOD=20``.
"""

import os


def _env(name, default=None):
    return os.environ.get(name, default)


def _env_int(name, default):
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _env_float(name, default):
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


# ---------------------------------------------------------------------------
# Sidecar -> ILS
# ---------------------------------------------------------------------------

CDS_SIP2_ILS_URL = _env("CDS_SIP2_ILS_URL", "http://localhost:5000/api/sip2")
"""Base URL of the SIP2-facing REST endpoints on the ILS (no trailing slash)."""

CDS_SIP2_ILS_TOKEN = _env("CDS_SIP2_ILS_TOKEN", "")
"""Bearer token the ILS issues to this sidecar. Always set from a Secret."""

CDS_SIP2_ILS_CONNECT_TIMEOUT = _env_float("CDS_SIP2_ILS_CONNECT_TIMEOUT", 3.0)
CDS_SIP2_ILS_READ_TIMEOUT = _env_float("CDS_SIP2_ILS_READ_TIMEOUT", 8.0)
"""HTTP timeouts. Keep connect + read well below SIP2_TIMEOUT_PERIOD, or the
device gives up and re-sends while the ILS is still processing."""

# Fallbacks used when the ILS login response does not provide them.
CDS_SIP2_INSTITUTION_ID = _env("CDS_SIP2_INSTITUTION_ID", "CERN")
CDS_SIP2_LIBRARY_NAME = _env("CDS_SIP2_LIBRARY_NAME", "CERN Library")
CDS_SIP2_LANGUAGE = _env("CDS_SIP2_LANGUAGE", "en")

CDS_SIP2_MESSAGES = {
    "unavailable": "Service temporarily unavailable. Please ask at the library desk.",
    "unknown_patron": "Card not recognised. Please ask at the library desk.",
    "unknown_item": "Item not found.",
    "refused": "Transaction refused. Please ask at the library desk.",
}
"""Screen messages (SIP2 field AF) shown on the device for sidecar-side failures.
ILS-side rejections carry their own ``user_message``."""

CDS_SIP2_MONITORING_TOKEN = _env("CDS_SIP2_MONITORING_TOKEN")
"""Optional shared secret for invenio-sip2's monitoring REST views. Unset ->
those views deny everything (the sidecar normally serves no HTTP at all)."""

# ---------------------------------------------------------------------------
# invenio-sip2
# ---------------------------------------------------------------------------

SIP2_REMOTE_ACTION_HANDLERS = {
    # Key = remote app name, passed to `invenio selfcheck start ... -r ils`.
    "ils": {
        "login_handler": "cds_sip2.handlers:selfcheck_login",
        "system_status_handler": "cds_sip2.handlers:system_status",
        "patron_handlers": {
            "validate_patron": "cds_sip2.handlers:validate_patron",
            "authorize_patron": "cds_sip2.handlers:authorize_patron",
            "enable_patron": "cds_sip2.handlers:enable_patron",
            "patron_status": "cds_sip2.handlers:patron_status",
            "account": "cds_sip2.handlers:patron_information",
        },
        "item_handlers": {
            "item": "cds_sip2.handlers:item_information",
        },
        "circulation_handlers": {
            "checkout": "cds_sip2.handlers:checkout",
            "checkin": "cds_sip2.handlers:checkin",
            "renew": "cds_sip2.handlers:renew",
            # "hold" / "renew_all" deliberately absent: not advertised in 98/BX.
        },
        # "fee_paid_handler" deliberately absent.
    }
}

SIP2_DATASTORE_HANDLER = "invenio_sip2.datastore:Sip2RedisDatastore"
SIP2_DATASTORE_REDIS_URL = _env("SIP2_DATASTORE_REDIS_URL", "redis://localhost:6379/0")
SIP2_DATASTORE_REDIS_PREFIX = _env("SIP2_DATASTORE_REDIS_PREFIX", "cds_sip2")

# Logging: every request/response frame is logged at INFO (that is the "simple
# log interface"). Frames contain patron barcodes and the login action logs the
# terminal password at DEBUG, so never run DEBUG in production.
SIP2_LOGGING_CONSOLE = True
SIP2_LOGGING_CONSOLE_LEVEL = _env("SIP2_LOGGING_CONSOLE_LEVEL", "INFO")
# The Dockerfile sets CDS_SIP2_LOG_FILE=/var/log/sip2/sip2.log.
SIP2_LOGGING_FS_LOGFILE = _env("CDS_SIP2_LOG_FILE") or None
SIP2_LOGGING_FS_LEVEL = "INFO"
SIP2_LOGGING_FS_MAXBYTES = 50 * 1024 * 1024
SIP2_LOGGING_FS_BACKUPCOUNT = 5

# Capabilities advertised in the ACS Status (98) response.
SIP2_SUPPORT_CHECKIN = True
SIP2_SUPPORT_CHECKOUT = True
SIP2_SUPPORT_RENEWAL_POLICY = True
SIP2_SUPPORT_STATUS_UPDATE = False
SIP2_SUPPORT_OFFLINE_STATUS = False
SIP2_SUPPORT_ONLINE_STATUS = True

SIP2_PROTOCOL = "2.00"
SIP2_TEXT_ENCODING = "UTF-8"
SIP2_LINE_TERMINATOR = "\r"
SIP2_ERROR_DETECTION = True  # expect and emit AY<seq>AZ<checksum>
SIP2_TIMEOUT_PERIOD = _env_int("SIP2_TIMEOUT_PERIOD", 15)
SIP2_RETRIES_ALLOWED = _env_int("SIP2_RETRIES_ALLOWED", 3)
SIP2_DEFAULT_LANGUAGE = CDS_SIP2_LANGUAGE
BABEL_DEFAULT_LANGUAGE = CDS_SIP2_LANGUAGE

SIP2_PERMISSIONS_FACTORY = "cds_sip2.permissions:permission_factory"

# ---------------------------------------------------------------------------
# Inert defaults for Invenio extensions that arrive transitively (invenio-app,
# invenio-access -> invenio-accounts -> invenio-db, ...). None of them is used
# by the sidecar; these values only stop their init_app() from complaining or
# from trying to reach services that do not exist in this pod.
# ---------------------------------------------------------------------------
SECRET_KEY = _env("SECRET_KEY", "dev-only-change-me")
SQLALCHEMY_DATABASE_URI = _env("SQLALCHEMY_DATABASE_URI", "sqlite://")
RATELIMIT_ENABLED = False
RATELIMIT_STORAGE_URI = "memory://"
RATELIMIT_STORAGE_URL = "memory://"  # older Flask-Limiter name
CACHE_TYPE = "NullCache"
ACCOUNTS_SESSION_REDIS_URL = SIP2_DATASTORE_REDIS_URL
APP_ALLOWED_HOSTS = None
