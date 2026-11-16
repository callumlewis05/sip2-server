"""SIP2 error detection: the ``AY<seq>AZ<checksum>`` trailer.

The checksum is the two's complement of the 16-bit byte sum of everything
up to and including ``AZ``, as four upper-case hex digits. It is computed
on bytes, so it is independent of the text encoding.
"""

import re

_TRAILER = re.compile(rb"(?:AY(\d))?AZ([0-9A-Fa-f]{4})$")


def compute(data):
    """Checksum of ``data`` (bytes ending in ``AZ``)."""
    return f"{(-sum(data)) & 0xFFFF:04X}"


def verify(data, checksum):
    """True if ``checksum`` matches ``data`` (bytes ending in ``AZ``)."""
    return (sum(data) + int(checksum, 16)) & 0xFFFF == 0


def split(frame):
    """Split a frame (no terminator) into ``(body, sequence, checksum_ok)``.

    ``sequence`` and ``checksum_ok`` are ``None`` when the frame carries no
    trailer.
    """
    match = _TRAILER.search(frame)
    if not match:
        return frame, None, None
    sequence = int(match.group(1)) if match.group(1) is not None else None
    ok = verify(frame[: match.start(2)], match.group(2).decode())
    return frame[: match.start()], sequence, ok


def append(body, sequence):
    """Add the trailer to ``body``; ``AY`` only when the request carried one."""
    head = body + (b"AY%d" % sequence if sequence is not None else b"") + b"AZ"
    return head + compute(head).encode()
