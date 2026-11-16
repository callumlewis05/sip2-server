"""Checksum, decoding, encoding, sanitising and redaction."""

import pytest

from cds_sip2.sip2 import checksum, codec
from cds_sip2.sip2.codec import FrameError, ResendError, build, decode, encode, redact
from tests import sip2_client

NOW = "20261102    120000"


def wire(body, sequence=0):
    """A request frame as the test client sends it, without the CR."""
    return sip2_client.frame(body, sequence).rstrip("\r").encode()


# ------------------------------------------------------------------ checksum


def test_checksum_matches_the_reference_client():
    partial = b"9300CNkiosk1|COsecret|CP|AY3AZ"
    assert checksum.compute(partial) == sip2_client.checksum(partial.decode())
    assert checksum.verify(partial, checksum.compute(partial))


def test_split_trailer():
    body, sequence, ok = checksum.split(wire("9900402.00", 7))
    assert (body, sequence, ok) == (b"9900402.00", 7, True)
    assert checksum.split(b"9900402.00") == (b"9900402.00", None, None)


def test_append_without_sequence():
    framed = checksum.append(b"941", None)
    assert framed.startswith(b"941AZ")
    assert checksum.split(framed) == (b"941", None, True)


# ------------------------------------------------------------------- decode


def test_decode_fixed_and_repeated_variable_fields():
    req = decode(wire(f"63001{NOW}  Y       AOCERN|AAP1|ADpin|AUx|AUy|", 4))

    assert req.code == "63"
    assert req.name == "Patron Information"
    assert req.sequence == 4
    assert req.fixed == {"language": "001", "transaction_date": NOW, "summary": "  Y       "}
    assert req.get("AA") == "P1"
    assert req.fields["AU"] == ("x", "y")
    assert req.has("AD") and not req.has("AC")
    assert req.get("ZZ", "default") == "default"


def test_decode_utf8():
    assert decode(wire(f"17{NOW}AOCERN|ABÉcoute|")).get("AB") == "Écoute"


def test_bad_checksum_asks_for_resend():
    tampered = wire("9900402.00")[:-4] + b"0000"
    with pytest.raises(ResendError):
        decode(tampered)
    # ...unless error detection is off.
    assert decode(tampered, error_detection=False).code == "99"


def test_missing_trailer_is_accepted():
    req = decode(b"9900402.00")
    assert req.code == "99" and req.sequence is None


def test_truncated_fixed_part_asks_for_resend():
    with pytest.raises(ResendError):
        decode(wire("11YN2026"))


@pytest.mark.parametrize("frame", [b"\xff\xfe\x00garbage", b"GET / HTTP/1.1", b"", b"9"])
def test_garbage_is_a_frame_error(frame):
    with pytest.raises(FrameError):
        decode(frame)


def test_unknown_code_decodes_without_fields():
    req = decode(wire("42whatever|"))
    assert req.code == "42" and req.fields == {}


# ------------------------------------------------------------------- encode


def test_encode_has_valid_trailer_and_parses_in_reference_client():
    response = build("94", {"ok": "1"})
    raw = encode(response, sequence=5).decode()

    parsed = sip2_client.parse(raw)
    assert raw.endswith("\r")
    assert parsed.code == "94" and parsed.ok
    assert parsed.sequence == 5
    assert parsed.checksum_ok is True


def test_encode_without_error_detection():
    assert encode(build("94", {"ok": "0"}), 5, error_detection=False) == b"940\r"


def test_build_orders_fields_and_fills_required():
    response = build("36", {"end_session": "Y", "transaction_date": NOW}, AF="bye", AA="P1")
    assert response.fields == [("AO", ""), ("AA", "P1"), ("AF", "bye")]


def test_build_rejects_wrong_fixed_fields():
    with pytest.raises(ValueError):
        build("94", {"ok": "10"})
    with pytest.raises(ValueError):
        build("94", {})


def test_build_drops_none_and_repeats_lists():
    response = build(
        "36",
        {"end_session": "Y", "transaction_date": NOW},
        AO="CERN",
        AA="P1",
        AF=["one", "two"],
        AG=None,
    )
    assert response.fields == [("AO", "CERN"), ("AA", "P1"), ("AF", "one"), ("AF", "two")]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Title | with pipe", "Title  with pipe"),
        ("line\r\nbreak\ttab", "line  break tab"),
        ("bell\x07null\x00esc\x1b", "bellnullesc"),
        ("Écoute", "Écoute"),
        (None, ""),
        (42, "42"),
    ],
)
def test_sanitise(value, expected):
    assert codec.sanitise(value) == expected


def test_sanitise_limits_length():
    assert len(codec.sanitise("x" * 1000)) == codec.MAX_FIELD_LENGTH


def test_ils_text_cannot_break_the_frame():
    """A hostile title must not inject fields or end the frame early."""
    response = build(
        "18",
        {
            "circulation_status": "03",
            "security_marker": "00",
            "fee_type": "01",
            "transaction_date": NOW,
        },
        AB="IT1",
        AJ="Evil|AFinjected\rAY9AZ0000",
    )
    raw = encode(response, 1).decode()

    assert raw.count("\r") == 1
    parsed = sip2_client.parse(raw)
    assert parsed.checksum_ok is True
    assert parsed.all("AF") == []
    assert parsed.get("AJ") == "EvilAFinjected AY9AZ0000"


# ------------------------------------------------------------------- redact


@pytest.mark.parametrize(
    "body",
    [
        "9300CNkiosk1|COs3cr3t|CPloc|",
        f"63001{NOW}{' ' * 10}AOCERN|AAP001|ACs3cr3t|ADs3cr3t|",
        f"11YN{NOW}{' ' * 18}AOCERN|AAP001|ABIT1|ACs3cr3t|ADs3cr3t|",
        f"23001{NOW}AOCERN|AAP001|ACs3cr3t|ADs3cr3t",  # no trailing pipe
    ],
)
def test_redact_masks_secrets_and_pseudonymises_patron(body):
    text = redact(wire(body))
    assert "s3cr3t" not in text
    assert "P001" not in text
    assert "***" in text


def test_redact_drops_personal_details_from_responses():
    frame = (
        f"64              001{NOW}"
        + "0000" * 6
        + "AOCERN|AAP001|AEAda Lovelace|BEada@example.org|BDSomewhere|BF123|BLY|"
    )
    text = redact(frame.encode())
    for secret in ("Ada", "ada@", "Somewhere", "123", "P001"):
        assert secret not in text
    assert "BLY" in text


@pytest.mark.parametrize("frame", [b"42ADsecret|", b"63ADsecret", b"\xffADsecret"])
def test_redact_withholds_frames_it_cannot_parse(frame):
    assert "secret" not in redact(frame)


def test_redact_keeps_non_personal_frames_readable():
    assert redact(wire(f"17{NOW}AOCERN|ABIT1|", 2)).startswith(f"17{NOW}AOCERN|ABIT1|AY2AZ")
