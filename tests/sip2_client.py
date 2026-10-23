#!/usr/bin/env python3
"""Scripted SIP2 client.

Standard library only, so it runs anywhere: against a local server, against
the OpenShift route through stunnel (``--tls --cafile ...``), or inside the
integration tests.  It speaks SIP2 v2.00 with error detection (``AY<seq>AZ<checksum>``).

    python -m tests.sip2_client --host 127.0.0.1 --port 3004 \
        --terminal kiosk1 --password secret --patron P001 --pin 1234 --item CM-1234567890

Message builders return the *body* only; :class:`Sip2Connection` adds the
sequence number, checksum and ``\\r`` terminator.
"""

import argparse
import re
import socket
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

TERMINATOR = "\r"
DATE_FORMAT = "%Y%m%d    %H%M%S"  # YYYYMMDDZZZZHHMMSS, ZZZZ = 4 blanks (local time)
BLANK_DATE = " " * 18

# Response code -> ordered fixed fields (name, width). Variable fields follow.
FIXED_FIELDS: Dict[str, List] = {
    "94": [("ok", 1)],
    "96": [],
    "98": [
        ("online_status", 1), ("checkin_ok", 1), ("checkout_ok", 1),
        ("acs_renewal_policy", 1), ("status_update_ok", 1), ("offline_ok", 1),
        ("timeout_period", 3), ("retries_allowed", 3), ("date_time_sync", 18),
        ("protocol_version", 4),
    ],
    "24": [("patron_status", 14), ("language", 3), ("transaction_date", 18)],
    "26": [("patron_status", 14), ("language", 3), ("transaction_date", 18)],
    "64": [
        ("patron_status", 14), ("language", 3), ("transaction_date", 18),
        ("hold_items_count", 4), ("overdue_items_count", 4), ("charged_items_count", 4),
        ("fine_items_count", 4), ("recall_items_count", 4), ("unavailable_holds_count", 4),
    ],
    "18": [("circulation_status", 2), ("security_marker", 2), ("fee_type", 2),
           ("transaction_date", 18)],
    "12": [("ok", 1), ("renewal_ok", 1), ("magnetic_media", 1), ("desensitize", 1),
           ("transaction_date", 18)],
    "30": [("ok", 1), ("renewal_ok", 1), ("magnetic_media", 1), ("desensitize", 1),
           ("transaction_date", 18)],
    "10": [("ok", 1), ("resensitize", 1), ("magnetic_media", 1), ("alert", 1),
           ("transaction_date", 18)],
    "36": [("end_session", 1), ("transaction_date", 18)],
    "38": [("payment_accepted", 1), ("transaction_date", 18)],
}

_TRAILER = re.compile(r"AY(\d)AZ([0-9A-Fa-f]{4})$")


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------


def checksum(text: str, encoding: str = "utf-8") -> str:
    """SIP2 checksum of ``text`` (which must end with ``AZ``).

    Sum of all bytes, lower 16 bits, two's complement, four upper-case hex
    digits. Adding the checksum back to the byte sum therefore gives 0 mod 2**16.
    """
    total = sum(text.encode(encoding))
    return f"{(-total) & 0xFFFF:04X}"


def frame(body: str, sequence: int, error_detection: bool = True, encoding: str = "utf-8") -> str:
    """Add ``AY<seq>AZ<checksum>`` (unless disabled) and the terminator."""
    if not error_detection or body == "97":  # 97 Request ACS Resend carries no AY/AZ
        return body + TERMINATOR
    partial = f"{body}AY{sequence % 10}AZ"
    return partial + checksum(partial, encoding) + TERMINATOR


def now() -> str:
    return datetime.now().strftime(DATE_FORMAT)


# ---------------------------------------------------------------------------
# request builders (bodies only)
# ---------------------------------------------------------------------------


def login(user: str, password: str, location: str = "") -> str:
    """93 Login. UID/PWD algorithm '0' = plaintext."""
    return f"9300CN{user}|CO{password}|CP{location}|"


def sc_status(status: str = "0", print_width: str = "040", version: str = "2.00") -> str:
    """99 SC Status. status: 0 ok, 1 printer out of paper, 2 about to shut down."""
    return f"99{status}{print_width}{version}"


def patron_information(patron: str, institution: str = "", terminal_pwd: Optional[str] = None,
                       pin: Optional[str] = None, language: str = "001",
                       summary: str = " " * 10) -> str:
    """63 Patron Information. ``summary`` positions: hold/overdue/charged/fine/recall/unavail."""
    body = f"63{language}{now()}{summary}AO{institution}|AA{patron}|"
    if terminal_pwd is not None:
        body += f"AC{terminal_pwd}|"
    if pin is not None:
        body += f"AD{pin}|"
    return body


def patron_status_request(patron: str, institution: str = "", terminal_pwd: str = "",
                          pin: str = "", language: str = "001") -> str:
    """23 Patron Status Request."""
    return f"23{language}{now()}AO{institution}|AA{patron}|AC{terminal_pwd}|AD{pin}|"


def item_information(item: str, institution: str = "") -> str:
    """17 Item Information."""
    return f"17{now()}AO{institution}|AB{item}|"


def checkout(patron: str, item: str, institution: str = "", terminal_pwd: str = "",
             pin: Optional[str] = None, renewal_policy: str = "Y", no_block: str = "N") -> str:
    """11 Checkout."""
    body = (f"11{renewal_policy}{no_block}{now()}{BLANK_DATE}"
            f"AO{institution}|AA{patron}|AB{item}|AC{terminal_pwd}|")
    if pin is not None:
        body += f"AD{pin}|"
    return body


def checkin(item: str, institution: str = "", location: str = "", terminal_pwd: str = "",
            no_block: str = "N") -> str:
    """09 Checkin."""
    return (f"09{no_block}{now()}{now()}"
            f"AP{location}|AO{institution}|AB{item}|AC{terminal_pwd}|")


def renew(patron: str, item: str, institution: str = "", terminal_pwd: str = "",
          pin: Optional[str] = None, third_party: str = "N", no_block: str = "N") -> str:
    """29 Renew."""
    body = (f"29{third_party}{no_block}{now()}{BLANK_DATE}"
            f"AO{institution}|AA{patron}|AB{item}|AC{terminal_pwd}|")
    if pin is not None:
        body += f"AD{pin}|"
    return body


def end_patron_session(patron: str, institution: str = "") -> str:
    """35 End Patron Session."""
    return f"35{now()}AO{institution}|AA{patron}|"


def request_resend() -> str:
    """97 Request ACS Resend."""
    return "97"


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------


@dataclass
class Sip2Response:
    raw: str
    code: str
    fixed: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, List[str]] = field(default_factory=dict)
    sequence: Optional[int] = None
    checksum_ok: Optional[bool] = None  # None when the server sent no AY/AZ

    def get(self, tag: str, default=None):
        values = self.fields.get(tag)
        return values[0] if values else default

    def all(self, tag: str) -> List[str]:
        return self.fields.get(tag, [])

    @property
    def ok(self) -> bool:
        return self.fixed.get("ok") == "1"

    def __str__(self) -> str:
        fixed = " ".join(f"{k}={v!r}" for k, v in self.fixed.items())
        var = " ".join(f"{k}={v!r}" for k, vals in self.fields.items() for v in vals)
        return f"<{self.code} {fixed} | {var}>"


def parse(raw: str, encoding: str = "utf-8") -> Sip2Response:
    """Parse one response frame (with or without the trailing ``\\r``)."""
    raw = raw.rstrip("\r\n")
    body, sequence, checksum_ok = raw, None, None
    match = _TRAILER.search(raw)
    if match:
        sequence = int(match.group(1))
        head = raw[: match.start(2)]  # up to and including "AZ"
        checksum_ok = checksum(head, encoding) == match.group(2).upper()
        body = raw[: match.start()]

    code = body[:2]
    fixed: Dict[str, str] = {}
    pos = 2
    for name, width in FIXED_FIELDS.get(code, []):
        fixed[name] = body[pos: pos + width]
        pos += width

    fields: Dict[str, List[str]] = {}
    for part in body[pos:].split("|"):
        if len(part) >= 2:
            fields.setdefault(part[:2], []).append(part[2:])
    return Sip2Response(raw=raw, code=code, fixed=fixed, fields=fields,
                        sequence=sequence, checksum_ok=checksum_ok)


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------


class Sip2Connection:
    """One TCP (optionally TLS) connection with an auto-incrementing sequence number."""

    def __init__(self, host: str, port: int, tls: bool = False, cafile: Optional[str] = None,
                 insecure: bool = False, timeout: float = 10.0, encoding: str = "utf-8",
                 error_detection: bool = True, verbose: bool = False):
        self.encoding = encoding
        self.error_detection = error_detection
        self.verbose = verbose
        self.sequence = 0
        sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            context = ssl.create_default_context(cafile=cafile)
            if insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=host)
        self.sock = sock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, body: str) -> Sip2Response:
        """Frame ``body``, send it, read one response frame, parse it."""
        wire = frame(body, self.sequence, self.error_detection, self.encoding)
        if self.verbose:
            print(f">> {wire.rstrip(TERMINATOR)}")
        self.sock.sendall(wire.encode(self.encoding))
        raw = self._read_frame()
        if self.verbose:
            print(f"<< {raw.rstrip(TERMINATOR)}")
        if body != "97":
            self.sequence = (self.sequence + 1) % 10
        return parse(raw, self.encoding)

    def _read_frame(self) -> str:
        buffer = b""
        while not buffer.endswith(TERMINATOR.encode(self.encoding)):
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed by server")
            buffer += chunk
        return buffer.decode(self.encoding)


# ---------------------------------------------------------------------------
# scripted session
# ---------------------------------------------------------------------------


def run_session(conn: Sip2Connection, terminal: str, password: str, location: str = "",
                institution: str = "", patron: Optional[str] = None, pin: Optional[str] = None,
                item: Optional[str] = None, do_checkin: bool = True, do_renew: bool = False):
    """93 → 99 → [63 → 17 → 11 → (29) → (09) → 35].

    Returns ``[(step, ok, response)]``.
    """
    results = []

    def step(name, body, check):
        response = conn.send(body)
        ok = False
        try:
            ok = bool(check(response))
        except Exception:  # noqa: BLE001 - a parse failure is a failed step
            ok = False
        results.append((name, ok, response))
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {response}")
        return response

    login_resp = step("93 login", login(terminal, password, location),
                      lambda r: r.code == "94" and r.ok)
    if not login_resp.ok:
        return results
    status = step("99 sc status", sc_status(),
                  lambda r: r.code == "98" and r.fixed["online_status"] == "Y")
    institution = institution or status.get("AO", "")

    if patron:
        step("63 patron information", patron_information(patron, institution, pin=pin),
             lambda r: r.code == "64" and r.get("BL") == "Y"
             and (pin is None or r.get("CQ") == "Y"))
    if item:
        step("17 item information", item_information(item, institution),
             lambda r: r.code == "18" and r.get("AB") == item)
    if patron and item:
        step("11 checkout", checkout(patron, item, institution, pin=pin),
             lambda r: r.code == "12" and r.ok and bool(r.get("AH")))
        if do_renew:
            step("29 renew", renew(patron, item, institution, pin=pin),
                 lambda r: r.code == "30" and r.ok)
        if do_checkin:
            step("09 checkin", checkin(item, institution),
                 lambda r: r.code == "10" and r.ok)
    if patron:
        step("35 end patron session", end_patron_session(patron, institution),
             lambda r: r.code == "36" and r.fixed["end_session"] == "Y")
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3004)
    parser.add_argument("--tls", action="store_true", help="wrap the socket in TLS (stunnel route)")
    parser.add_argument("--cafile", help="CA bundle for --tls")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--no-error-detection", action="store_true",
                        help="do not send AY/AZ (server must have SIP2_ERROR_DETECTION=False)")
    parser.add_argument("--terminal", required=True, help="SIP2 login (CN)")
    parser.add_argument("--password", required=True, help="SIP2 password (CO)")
    parser.add_argument("--location", default="", help="location code (CP)")
    parser.add_argument("--institution", default="", help="institution id (AO); default from 98")
    parser.add_argument("--patron", help="patron barcode (AA)")
    parser.add_argument("--pin", help="patron PIN (AD)")
    parser.add_argument("--item", help="item barcode (AB)")
    parser.add_argument("--no-checkin", action="store_true", help="leave the item checked out")
    parser.add_argument("--renew", action="store_true", help="also send 29 Renew")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print raw frames")
    args = parser.parse_args(argv)

    with Sip2Connection(args.host, args.port, tls=args.tls, cafile=args.cafile,
                        insecure=args.insecure, timeout=args.timeout, encoding=args.encoding,
                        error_detection=not args.no_error_detection,
                        verbose=not args.quiet) as conn:
        results = run_session(conn, args.terminal, args.password, args.location,
                              args.institution, args.patron, args.pin, args.item,
                              do_checkin=not args.no_checkin, do_renew=args.renew)
    failed = [name for name, ok, _ in results if not ok]
    bad_checksums = [name for name, _, r in results if r.checksum_ok is False]
    if bad_checksums:
        print(f"WARNING: bad checksum on {', '.join(bad_checksums)}")
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed")
    return 1 if failed or bad_checksums else 0


if __name__ == "__main__":
    sys.exit(main())
