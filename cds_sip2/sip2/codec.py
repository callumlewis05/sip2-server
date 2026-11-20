"""Frame <-> typed objects, with sanitising on the way out and redaction for logs.

* :func:`decode` turns a received frame into a :class:`Request`, raising
  :class:`FrameError` (close the connection) or :class:`ResendError` (answer
  ``96``).
* :func:`encode` turns a :class:`Response` into bytes. Every variable field
  is stripped of ``|`` and control characters and length-limited, because
  titles, names and messages come from the ILS.
* :func:`redact` is the only way a frame may reach a log line. It works from
  an allow-list: tags known to carry no personal data are shown, patron
  barcodes are pseudonymised, and every other tag is masked. A new tag is
  therefore hidden until someone decides it is safe to log.

:class:`Request` and :class:`Response` are pure wire objects. Typed views of
them, per message, live in :mod:`cds_sip2.sip2.mapper`.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from ..logging import pseudonymise
from . import checksum
from .messages import REQUESTS, RESPONSES

MAX_FIELD_LENGTH = 255

LOGGABLE_TAGS = frozenset(
    {
        "AO",  # institution id
        "AB",  # item barcode
        "AJ",  # title
        "AH",  # due date
        "AP",  # current location
        "AQ",  # permanent location
        "AM",  # library name
        "AN",  # terminal location
        "BG",  # owner
        "BL",  # valid patron Y/N
        "BX",  # supported messages
        "CF",  # hold queue length
        "CK",  # media type
        "CN",  # terminal (login user id)
        "CP",  # location code
        "CQ",  # valid patron password Y/N
        "AY",  # sequence number
        "AZ",  # checksum
    }
)
"""Tags shown as-is in logs. Everything else is masked as ``XX~``."""
PSEUDONYMISED_TAGS = frozenset({"AA"})
"""Patron barcode: logged as a keyed hash."""

UNTRAILED = frozenset({"97"})
"""Requests that legitimately carry no AY/AZ, even with error detection on."""

_CODE = re.compile(r"^\d\d")


class FrameError(Exception):
    """Not a SIP2 frame at all. The connection should be closed."""


class ResendError(Exception):
    """Bad checksum or truncated message: answer ``96`` and let the client resend."""


@dataclass(frozen=True)
class Request:
    code: str
    fixed: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    """tag -> tuple of values, in arrival order (tags may repeat)."""
    sequence: int | None = None
    raw: bytes = b""

    def get(self, tag, default=""):
        values = self.fields.get(tag)
        return values[0] if values else default

    def has(self, tag):
        return tag in self.fields

    @property
    def name(self):
        spec = REQUESTS.get(self.code)
        return spec.name if spec else "unknown"


@dataclass
class Response:
    code: str
    fixed: dict
    fields: list
    """``[(tag, value), ...]`` in emission order."""


def build(code, fixed, **fields):
    """Assemble a :class:`Response` in spec order.

    ``None`` values are dropped, lists become repeated fields, and every
    required tag is present (empty if not given).
    """
    spec = RESPONSES[code]
    names = [name for name, _ in spec.fixed]
    if set(fixed) != set(names):
        raise ValueError(f"{code}: fixed fields {sorted(fixed)} != {sorted(names)}")
    for name, width in spec.fixed:
        if len(fixed[name]) != width:
            raise ValueError(f"{code}.{name}: {fixed[name]!r} is not {width} chars")
    for tag in spec.required:
        if fields.get(tag) is None:
            fields[tag] = ""
    order = list(spec.fields) + sorted(set(fields) - set(spec.fields))
    ordered = []
    for tag in order:
        value = fields.get(tag)
        if value is None:
            continue
        for item in value if isinstance(value, (list, tuple)) else (value,):
            ordered.append((tag, item))
    return Response(code, {name: fixed[name] for name in names}, ordered)


def sanitise(value, max_length=MAX_FIELD_LENGTH):
    """Make ``value`` safe inside a variable field."""
    cleaned = []
    for ch in "" if value is None else str(value):
        if ch == "|":
            continue
        if unicodedata.category(ch) == "Cc":
            if ch in "\t\n\r":
                cleaned.append(" ")
            continue
        cleaned.append(ch)
    return "".join(cleaned)[:max_length]


def decode(frame, error_detection=True, encoding="utf-8", require_checksum=False):
    """Parse one received frame (no terminator).

    With ``error_detection``, a wrong checksum asks for a resend. With
    ``require_checksum`` as well, so does a missing trailer (except on
    :data:`UNTRAILED` messages).
    """
    body, sequence, checksum_ok = checksum.split(frame)
    if error_detection and checksum_ok is False:
        raise ResendError("bad checksum")
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError:
        raise FrameError(f"not valid {encoding}") from None
    if not _CODE.match(text):
        raise FrameError("does not start with a message code")
    code = text[:2]
    if error_detection and require_checksum and checksum_ok is None and code not in UNTRAILED:
        raise ResendError("missing checksum")
    spec = REQUESTS.get(code)
    if spec is None:
        return Request(code=code, sequence=sequence, raw=frame)
    if len(text) < 2 + spec.fixed_width:
        raise ResendError(f"{code}: fixed part truncated")

    fixed, pos = {}, 2
    for name, width in spec.fixed:
        fixed[name] = text[pos : pos + width]
        pos += width
    fields = {}
    for part in text[pos:].split("|"):
        if len(part) >= 2:
            fields.setdefault(part[:2], []).append(part[2:])
    return Request(
        code=code,
        fixed=fixed,
        fields={tag: tuple(values) for tag, values in fields.items()},
        sequence=sequence,
        raw=frame,
    )


def encode(response, sequence=None, error_detection=True, encoding="utf-8"):
    """Serialise ``response`` to wire bytes, terminator included."""
    spec = RESPONSES[response.code]
    parts = [response.code]
    for name, width in spec.fixed:
        value = response.fixed[name]
        if len(value) != width or sanitise(value) != value:
            raise ValueError(f"{response.code}.{name}: bad fixed value {value!r}")
        parts.append(value)
    parts.extend(f"{tag}{sanitise(value)}|" for tag, value in response.fields)
    body = "".join(parts).encode(encoding, errors="replace")
    if error_detection:
        body = checksum.append(body, sequence)
    return body + b"\r"


def redact(frame, encoding="utf-8"):
    """Loggable rendering of a request or response frame."""
    text = frame.decode(encoding, errors="replace").rstrip("\r\n")
    code = text[:2]
    spec = REQUESTS.get(code) or RESPONSES.get(code)  # the two code sets are disjoint
    if spec is None or len(text) < 2 + spec.fixed_width:
        # We cannot tell where the variable fields start, so show nothing.
        return f"{code!r} <{len(frame)} bytes withheld>"
    head = text[: 2 + spec.fixed_width]
    rendered = []
    for part in text[2 + spec.fixed_width :].split("|"):
        tag = part[:2]
        if len(part) <= 2 or tag in LOGGABLE_TAGS:
            pass
        elif tag in PSEUDONYMISED_TAGS:
            part = f"{tag}{pseudonymise(part[2:])}"
        else:
            part = f"{tag}~"
        rendered.append(part)
    return head + "|".join(rendered)
