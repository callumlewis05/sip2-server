"""Structured JSON logging to stdout, and pseudonymisation of patron barcodes.

Every record is one JSON object per line. Anything passed in ``extra=`` ends
up as a top-level key, so call sites log *events* with fields rather than
pre-formatted strings::

    logger.info("checkout refused", extra={"event": "checkout", "item": item,
                                           "patron": pseudonymise(patron)})

Frames are never logged raw: :func:`cds_sip2.sip2.codec.redact` is the only
way a frame reaches a log line.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sys
from datetime import UTC, datetime

_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

_hash_key = secrets.token_bytes(32)


class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure(level="INFO", hash_key="", stream=None):
    """Install the JSON handler on the root logger. Call once, at start-up."""
    global _hash_key
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs every request URL at INFO, and URLs contain patron barcodes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if hash_key:
        _hash_key = hash_key.encode()
    else:
        logging.getLogger(__name__).warning(
            "CDS_SIP2_LOG_HASH_KEY is not set: patron pseudonyms change on every restart",
            extra={"event": "config"},
        )


def pseudonymise(value):
    """Truncated keyed hash of a patron barcode, for logs.

    Support can correlate entries for one patron (and, given the barcode and
    the key, find them) without the log revealing who the patron is.
    """
    if not value:
        return value
    digest = hmac.new(_hash_key, str(value).encode(), hashlib.sha256).hexdigest()
    return f"p:{digest[:12]}"
