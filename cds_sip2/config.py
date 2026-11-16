"""Settings for the CDS SIP2 server.

Read once from the environment by :meth:`Settings.from_env` in the
composition root and passed down explicitly; nothing else reads
``os.environ``. The object is frozen, so a value cannot change while the
server is running.
"""

import os
from dataclasses import dataclass, field, fields
from types import MappingProxyType

DEFAULT_MESSAGES = MappingProxyType(
    {
        "unavailable": "Service temporarily unavailable. Please ask at the library desk.",
        "unknown_patron": "Card not recognised. Please ask at the library desk.",
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
    """An environment variable is missing or has an unusable value."""


@dataclass(frozen=True)
class Settings:
    # --- ILS (package ``ils``) ----------------------------------------------
    ils_url: str = "http://localhost:5000/api/sip2"
    ils_token: str = ""
    ils_connect_timeout: float = 3.0
    ils_read_timeout: float = 8.0
    """Keep connect + read well below the kiosk's own timeout, or the kiosk
    gives up and resends while the ILS is still processing."""

    # --- SIP2 listener ------------------------------------------------------
    bind_host: str = "127.0.0.1"
    """Loopback: stunnel in the same pod is the only client."""
    port: int = 3004
    max_connections: int = 50
    idle_timeout: float = 300.0
    """Seconds without a complete frame before the connection is closed."""
    max_frame_bytes: int = 4096
    shutdown_grace: float = 10.0
    """Seconds an in-flight request may take to finish after SIGTERM."""

    # --- SIP2 protocol ------------------------------------------------------
    error_detection: bool = True
    """Verify AZ on requests and emit AY/AZ on every response."""
    encoding: str = "utf-8"
    timeout_period: int = 15
    """Advertised verbatim in the 98 response (3 digits)."""
    retries_allowed: int = 3
    max_login_attempts: int = 3
    """Failed 93s on one connection before it is closed."""

    # --- Fallbacks when the ILS login response omits them -------------------
    institution_id: str = "CERN"
    library_name: str = "CERN Library"
    language: str = "en"

    # --- Health -------------------------------------------------------------
    health_host: str = "0.0.0.0"
    """The probes come from the kubelet, so this one is not loopback."""
    health_port: int = 8080
    health_ils_cache: float = 10.0
    """Seconds a readiness probe reuses the last ILS ping result."""

    # --- Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_hash_key: str = ""
    """Key for pseudonymising patron barcodes in logs. Set it from a Secret so
    entries stay correlatable across restarts; empty -> random per process."""

    messages: MappingProxyType = field(default=DEFAULT_MESSAGES)

    @classmethod
    def from_env(cls, environ=None):
        """Build settings from ``CDS_SIP2_*`` variables (see the README)."""
        env = os.environ if environ is None else environ
        values = {}
        for spec in fields(cls):
            if spec.name == "messages":
                continue
            name = f"CDS_SIP2_{spec.name.upper()}"
            raw = env.get(name)
            if raw is None or raw == "":
                continue
            values[spec.name] = _coerce(name, raw, type(spec.default))
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self):
        if self.max_connections < 1:
            raise ConfigError("CDS_SIP2_MAX_CONNECTIONS must be at least 1")
        if self.max_frame_bytes < 64:
            raise ConfigError("CDS_SIP2_MAX_FRAME_BYTES must be at least 64")
        if not 0 <= self.timeout_period <= 999 or not 0 <= self.retries_allowed <= 999:
            raise ConfigError("CDS_SIP2_TIMEOUT_PERIOD and _RETRIES_ALLOWED must fit 3 digits")
        try:
            "".encode(self.encoding)
        except LookupError as exc:
            raise ConfigError(f"CDS_SIP2_ENCODING: unknown encoding {self.encoding!r}") from exc

    def message(self, key):
        return self.messages.get(key) or self.messages["refused"]


def _coerce(name, raw, kind):
    if kind is bool:
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{name}: expected a boolean, got {raw!r}")
    try:
        return kind(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: expected {kind.__name__}, got {raw!r}") from exc
