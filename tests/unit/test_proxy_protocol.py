"""PROXY protocol v1 parsing and the loopback check that guards it."""

import pytest

from cds_sip2.sip2.proxy_protocol import MAX_HEADER, ProxyHeaderError, parse
from cds_sip2.utils import is_loopback


@pytest.mark.parametrize(
    "line, source",
    [
        (b"PROXY TCP4 192.0.2.10 10.0.0.1 51234 3004\r\n", ("192.0.2.10", 51234)),
        (b"PROXY TCP6 2001:db8::7 ::1 1 3004\r\n", ("2001:db8::7", 1)),
        (b"PROXY UNKNOWN\r\n", None),
        (b"PROXY UNKNOWN ffff::1 ffff::2 1 2\r\n", None),  # the rest is ignored
    ],
)
def test_valid_headers(line, source):
    assert parse(line) == source


def test_longest_valid_header_fits_the_limit():
    line = (
        b"PROXY TCP6 ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff "
        b"ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff 65535 65535\r\n"
    )
    assert len(line) <= MAX_HEADER
    assert parse(line) == ("ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", 65535)


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"9300CNkiosk1|COsecret|CP|\r",
        b"PROXY TCP4 192.0.2.10 10.0.0.1 51234 3004\n",  # LF without CR
        b"PROXY TCP4 192.0.2.10 10.0.0.1 51234\r\n",  # field missing
        b"PROXY TCP4 192.0.2.10  10.0.0.1 51234 3004\r\n",  # double space
        b"PROXY UDP4 192.0.2.10 10.0.0.1 51234 3004\r\n",
        b"PROXY TCP4 2001:db8::7 10.0.0.1 51234 3004\r\n",  # family mismatch
        b"PROXY TCP6 192.0.2.10 ::1 51234 3004\r\n",
        b"PROXY TCP4 192.0.2.300 10.0.0.1 51234 3004\r\n",
        b"PROXY TCP4 192.0.2.10 10.0.0.1 65536 3004\r\n",
        b"PROXY TCP4 192.0.2.10 10.0.0.1 -1 3004\r\n",
        b"PROXY TCP4 192.0.2.10 10.0.0.1 51234 3004\xff\r\n",
        b"PROXY " + b"x" * MAX_HEADER + b"\r\n",
        b"\r\n\r\n\x00\r\nQUIT\n",  # PROXY v2 signature: not supported
    ],
)
def test_malformed_headers_are_rejected(line):
    with pytest.raises(ProxyHeaderError):
        parse(line)


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1", True),
        ("127.4.5.6", True),
        ("::1", True),
        ("[::1]", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("10.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("ils.cern.ch", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected
