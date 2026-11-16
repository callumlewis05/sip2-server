"""CR-terminated SIP2 frames over an asyncio stream.

A frame is everything up to ``\\r``. A ``\\n`` straight after the ``\\r``
(some clients send CRLF) is dropped from the start of the next frame.
"""

import asyncio

TERMINATOR = b"\r"


class FrameTooLarge(Exception):
    """No terminator within the size limit. The connection must be closed."""


class ConnectionClosed(Exception):
    """The peer closed the connection. ``partial`` holds unterminated bytes."""

    def __init__(self, partial=b""):
        self.partial = partial
        super().__init__(f"closed with {len(partial)} unterminated bytes")


async def read_frame(reader, max_bytes):
    """Read one frame, without the terminator.

    The reader's own buffer limit (``start_server(limit=...)``) must be at
    least ``max_bytes`` so that oversize input surfaces here as
    ``FrameTooLarge`` rather than as unbounded buffering.
    """
    try:
        data = await reader.readuntil(TERMINATOR)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionClosed(exc.partial.lstrip(b"\n")) from None
    except asyncio.LimitOverrunError:
        raise FrameTooLarge() from None
    frame = data[:-1].lstrip(b"\n")
    if len(frame) > max_bytes:
        raise FrameTooLarge()
    return frame


def encode(body):
    return body + TERMINATOR
