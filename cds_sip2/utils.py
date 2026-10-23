"""Small helpers shared by the handlers."""

from datetime import date, datetime, time

DEFAULT_SIP2_DATE_FORMAT = "%Y%m%d    %H%M%S"
"""SIP2 18-char date: YYYYMMDDZZZZHHMMSS, with ZZZZ = four blanks (local time)."""


def normalize_barcode(value):
    """Normalise a barcode coming from the device (AA / AB fields)."""
    return "" if value is None else str(value).strip()


def compact(**kwargs):
    """Drop ``None`` values so they never become empty SIP2 fields."""
    return {key: value for key, value in kwargs.items() if value is not None}


def parse_iso(value):
    """Accept ISO-8601 strings, ``date`` or ``datetime``; return ``datetime`` or ``None``.

    Date-only values (``2026-11-04``) are read as end of day, which is how
    day-granular ILS due dates should appear on a kiosk.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59))
    text = str(value).strip()
    if text.endswith("Z"):  # Python < 3.11 fromisoformat does not accept Z
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if len(text) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


def to_sip2_date(value, fmt=None):
    """Format ``value`` as a SIP2 date, or return ``None`` when it cannot be parsed."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.strftime(fmt or _configured_date_format())


def _configured_date_format():
    try:
        from flask import current_app

        return current_app.config.get("SIP2_CIRCULATION_DATE_FORMAT") or DEFAULT_SIP2_DATE_FORMAT
    except RuntimeError:  # outside an application context
        return DEFAULT_SIP2_DATE_FORMAT
