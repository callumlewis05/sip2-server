"""PROXY protocol v1 header, as sent by stunnel's ``protocol = proxy``.

Behind stunnel every connection comes from loopback, so the ILS cannot tell
kiosks apart by address. With ``CDS_SIP2_PROXY_PROTOCOL`` on, stunnel opens
each connection with one text line naming the kiosk:

    PROXY TCP4 192.0.2.10 10.0.0.1 51234 3004\\r\\n

or ``PROXY UNKNOWN ...\\r\\n`` when it does not know. The line is at most
107 bytes (haproxy's proxy-protocol.txt, section 2.1). Only version 1 is
supported, because that is the only one stunnel sends. The server accepts
the header from loopback peers only: anything else could claim any address.
"""

import asyncio
import ipaddress

MAX_HEADER = 107
"""Longest valid v1 line, CRLF included."""

_SIGNATURE = b"PROXY "
_FAMILIES = {"TCP4": 4, "TCP6": 6}


class ProxyHeaderError(Exception):
    """Missing or malformed header. The connection must be closed."""


def parse(line):
    """``(source_ip, source_port)`` from a header line (CRLF included).

    Returns ``None`` for ``PROXY UNKNOWN``. Raises :class:`ProxyHeaderError`
    for anything that is not a well-formed v1 header.
    """
    if len(line) > MAX_HEADER or not line.startswith(_SIGNATURE) or not line.endswith(b"\r\n"):
        raise ProxyHeaderError("not a PROXY v1 header")
    try:
        parts = line[:-2].decode("ascii").split(" ")
    except UnicodeDecodeError:
        raise ProxyHeaderError("non-ASCII PROXY header") from None
    if parts[1:2] == ["UNKNOWN"]:
        return None
    if len(parts) != 6 or parts[1] not in _FAMILIES:
        raise ProxyHeaderError("malformed PROXY header")
    _, family, source, destination, source_port, destination_port = parts
    for address in (source, destination):
        try:
            version = ipaddress.ip_address(address).version
        except ValueError:
            raise ProxyHeaderError(f"bad address in PROXY header: {address!r}") from None
        if version != _FAMILIES[family]:
            raise ProxyHeaderError(f"{family} header with address {address!r}")
    for port in (source_port, destination_port):
        if not port.isdigit() or int(port) > 65535:
            raise ProxyHeaderError(f"bad port in PROXY header: {port!r}")
    return source, int(source_port)


async def read_header(reader, timeout):
    """Read and parse the header that must start the stream.

    The reader's buffer limit must be at least :data:`MAX_HEADER`.
    """
    try:
        async with asyncio.timeout(timeout):
            # Check the signature first, so a client talking SIP2 straight
            # away (no LF in sight) is refused at once, not after the timeout.
            start = await reader.readexactly(len(_SIGNATURE))
            if start != _SIGNATURE:
                raise ProxyHeaderError("not a PROXY v1 header")
            line = start + await reader.readuntil(b"\n")
    except TimeoutError:
        raise ProxyHeaderError("no PROXY header within timeout") from None
    except asyncio.IncompleteReadError:
        raise ProxyHeaderError("connection closed before PROXY header") from None
    except asyncio.LimitOverrunError:
        raise ProxyHeaderError("PROXY header too long") from None
    return parse(line)
