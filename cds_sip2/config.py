"""Configuration for the CDS SIP2 server.

Every setting is a module-level ``CDS_SIP2_*`` variable below, with its
default and a docstring, in the style of an Invenio ``config.py``. An
environment variable of the same name overrides it (an empty value keeps the
default). Values are coerced to the type of the default; mappings are read
as JSON objects and merged over the default.

:meth:`Settings.from_env` reads the environment once, in the composition
root, and the resulting frozen :class:`Settings` is passed down explicitly:
nothing else reads ``os.environ``, and nothing can change while the server
is running. ``Settings`` has one attribute per variable, named without the
prefix and in lower case (``CDS_SIP2_ILS_URL`` -> ``settings.ils_url``).
"""

import json
import os
from collections.abc import Mapping
from dataclasses import field, fields, make_dataclass
from types import MappingProxyType

PREFIX = "CDS_SIP2_"

# --- ILS (package ``rest``) --------------------------------------------------

CDS_SIP2_ILS_URL = "http://localhost:5000/api/sip2"
"""Base URL of the ILS ``/api/sip2`` endpoints."""

CDS_SIP2_ILS_TOKEN = ""
"""Bearer token identifying this service to the ILS. Supply it from an
OpenShift Secret; never commit it."""

CDS_SIP2_ILS_CONNECT_TIMEOUT = 3.0
CDS_SIP2_ILS_READ_TIMEOUT = 8.0
"""Per-operation httpx limits. The overall bound on a request is
``CDS_SIP2_REQUEST_DEADLINE``."""

# --- Terminals ---------------------------------------------------------------

CDS_SIP2_TERMINALS = MappingProxyType({})
"""Terminals allowed to log in with 93, as a JSON object (from a Secret)::

    {"kiosk1": {"password": "...", "user_pid": "42"}}

The key is the 93's ``CN``, ``password`` is checked against ``CO``, and
``user_pid`` is the ILS user the terminal's writes act as."""

# --- SIP2 listener -----------------------------------------------------------

CDS_SIP2_BIND_HOST = "127.0.0.1"
"""Loopback: stunnel in the same pod is the only client."""

CDS_SIP2_PORT = 3004
CDS_SIP2_MAX_CONNECTIONS = 50

CDS_SIP2_IDLE_TIMEOUT = 300.0
"""Seconds to wait for the next frame. Kiosks idle between patrons, so this
is long."""

CDS_SIP2_WRITE_TIMEOUT = 5.0
"""Seconds a reply may take to drain to the client. A stuck write fails fast."""

CDS_SIP2_MAX_FRAME_BYTES = 4096

CDS_SIP2_SHUTDOWN_GRACE = 10.0
"""Seconds an in-flight request may take to finish after SIGTERM."""

# --- SIP2 protocol -----------------------------------------------------------

CDS_SIP2_ERROR_DETECTION = True
"""Verify AZ on requests and emit AY/AZ on every response."""

CDS_SIP2_REQUIRE_CHECKSUM = False
"""With error detection on, also answer 96 to a request that has no AY/AZ
trailer at all (97 is exempt). Off by default for kiosks that omit it."""

CDS_SIP2_ENCODING = "utf-8"

CDS_SIP2_REQUEST_DEADLINE = 12.0
"""Seconds one request may take end to end, ILS calls included. On expiry the
kiosk gets a refusal (ambiguous for writes). Must be below
``CDS_SIP2_TIMEOUT_PERIOD`` so the server answers before the kiosk resends."""

CDS_SIP2_TIMEOUT_PERIOD = 15.0
"""Seconds the kiosk should wait for a reply, advertised in the 98 in tenths
of a second as SIP2 requires (15.0 -> ``150``). At most 99.9; 0 advertises
``000``."""

CDS_SIP2_RETRIES_ALLOWED = 3
CDS_SIP2_MAX_LOGIN_ATTEMPTS = 3
"""Failed 93s on one connection before it is closed."""

CDS_SIP2_DEFAULT_MEDIA_TYPE = "001"
"""SIP2 media type code (CK) for items whose ILS record has no media type."""

CDS_SIP2_SECURITY_MARKER = "00"
"""SIP2 security marker for every item: ``00`` other (RFID), ``01`` none,
``02`` tattle-tape, ``03`` whisper tape."""

# --- Session defaults --------------------------------------------------------

CDS_SIP2_INSTITUTION_ID = "CERN"
CDS_SIP2_LIBRARY_NAME = "CERN Library"
CDS_SIP2_LANGUAGE = "en"

# --- Health ------------------------------------------------------------------

CDS_SIP2_HEALTH_HOST = "0.0.0.0"
"""The probes come from the kubelet, so this one is not loopback."""

CDS_SIP2_HEALTH_PORT = 8080
CDS_SIP2_HEALTH_ILS_CACHE = 10.0
"""Seconds a readiness probe reuses the last ILS ping result."""

# --- Logging -----------------------------------------------------------------

CDS_SIP2_LOG_LEVEL = "INFO"
CDS_SIP2_LOG_HASH_KEY = ""
"""Key for pseudonymising patron barcodes in logs. Set it from a Secret so
entries stay correlatable across restarts; empty -> random per process."""

# --- Screen messages ---------------------------------------------------------

CDS_SIP2_MESSAGES = MappingProxyType(
    {
        "unavailable": "Service temporarily unavailable. Please ask at the library desk.",
        "unknown_item": "Item not found.",
        "refused": "Transaction refused. Please ask at the library desk.",
        "ambiguous": "Could not confirm the transaction. Please check at the library desk.",
        "not_logged_in": "Terminal not logged in. Please ask at the library desk.",
        "unsupported": "This function is not available here. Please ask at the library desk.",
    }
)
"""Screen messages (SIP2 field AF) for server-side outcomes. ILS refusals
carry their own ``user_message``."""


class ConfigError(ValueError):
    """A setting is missing or has an unusable value."""


def defaults():
    """``{attribute name: default}`` for every ``CDS_SIP2_*`` variable above."""
    return {
        name.removeprefix(PREFIX).lower(): value
        for name, value in globals().items()
        if name.startswith(PREFIX) and name.isupper()
    }


class _SettingsBase:
    @classmethod
    def from_env(cls, environ=None):
        """Build settings from the ``CDS_SIP2_*`` environment variables."""
        env = os.environ if environ is None else environ
        values = {}
        for spec in fields(cls):
            name = PREFIX + spec.name.upper()
            raw = env.get(name)
            if raw is None or raw == "":
                continue
            values[spec.name] = _coerce(name, raw, spec.default)
        return cls(**values)

    def __post_init__(self):
        for spec in fields(self):
            value = getattr(self, spec.name)
            if isinstance(spec.default, Mapping) and not isinstance(value, MappingProxyType):
                object.__setattr__(self, spec.name, MappingProxyType(dict(value)))
        self.validate()

    def validate(self):
        from .sip2.enums import MediaType, SecurityMarker

        if self.max_connections < 1:
            raise ConfigError("CDS_SIP2_MAX_CONNECTIONS must be at least 1")
        if self.max_frame_bytes < 64:
            raise ConfigError("CDS_SIP2_MAX_FRAME_BYTES must be at least 64")
        if not 0 <= self.timeout_period <= 99.9:
            raise ConfigError("CDS_SIP2_TIMEOUT_PERIOD must be between 0 and 99.9 seconds")
        if not 0 <= self.retries_allowed <= 999:
            raise ConfigError("CDS_SIP2_RETRIES_ALLOWED must fit 3 digits")
        if self.request_deadline <= 0:
            raise ConfigError("CDS_SIP2_REQUEST_DEADLINE must be positive")
        if self.timeout_period and self.request_deadline >= self.timeout_period:
            raise ConfigError(
                "CDS_SIP2_REQUEST_DEADLINE must be below CDS_SIP2_TIMEOUT_PERIOD, or the "
                "kiosk gives up and resends while the first request is still running"
            )
        if self.default_media_type not in set(MediaType):
            raise ConfigError("CDS_SIP2_DEFAULT_MEDIA_TYPE: unknown SIP2 media type")
        if self.security_marker not in set(SecurityMarker):
            raise ConfigError("CDS_SIP2_SECURITY_MARKER: unknown SIP2 security marker")
        for terminal, entry in self.terminals.items():
            if not (
                isinstance(entry, Mapping)
                and isinstance(entry.get("password"), str)
                and entry.get("password")
                and entry.get("user_pid") not in (None, "")
            ):
                raise ConfigError(
                    f"CDS_SIP2_TERMINALS[{terminal!r}] needs a non-empty password and user_pid"
                )
        try:
            "".encode(self.encoding)
        except LookupError as exc:
            raise ConfigError(f"CDS_SIP2_ENCODING: unknown encoding {self.encoding!r}") from exc

    def message(self, key):
        return self.messages.get(key) or self.messages["refused"]

    @property
    def advertised_timeout(self):
        """``timeout_period`` as the 98's 3-digit field, in tenths of a second."""
        return f"{round(self.timeout_period * 10):03d}"


Settings = make_dataclass(
    "Settings",
    [(name, type(value), field(default=value)) for name, value in defaults().items()],
    bases=(_SettingsBase,),
    frozen=True,
    module=__name__,
)
Settings.__doc__ = "Frozen settings: one attribute per ``CDS_SIP2_*`` variable in this module."


def _coerce(name, raw, default):
    kind = type(default)
    if kind is bool:
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected a boolean, got {raw!r}")
    if isinstance(default, Mapping):
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise ConfigError(f"{name}: expected a JSON object")
        return MappingProxyType({**default, **value})
    try:
        return kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: expected {kind.__name__}, got {raw!r}") from exc
