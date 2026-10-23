"""Unit tests for ``tests/sip2_client.py`` framing, checksum and parsing."""

import re

import pytest

from tests import sip2_client as sip2


def test_checksum_is_four_upper_hex_digits():
    cs = sip2.checksum("9300CNkiosk1|COsecret|CP|AY0AZ")
    assert re.fullmatch(r"[0-9A-F]{4}", cs)


def test_checksum_defining_property():
    """Byte sum of the message up to AZ plus the checksum value == 0 mod 2**16."""
    partial = "9300CNkiosk1|COsecret|CP|AY3AZ"
    cs = sip2.checksum(partial)
    assert (sum(partial.encode()) + int(cs, 16)) & 0xFFFF == 0


def test_frame_appends_sequence_checksum_and_terminator():
    wire = sip2.frame("9900402.00", sequence=7)
    assert wire.endswith("\r")
    body = wire[:-1]
    assert body.startswith("9900402.00AY7AZ")
    assert sip2.checksum(body[:-4]) == body[-4:]


def test_frame_without_error_detection_and_for_97():
    assert sip2.frame("9900402.00", 1, error_detection=False) == "9900402.00\r"
    assert sip2.frame("97", 1) == "97\r"


def test_sequence_wraps_mod_10():
    assert "AY2AZ" in sip2.frame("99", 12)


def test_parse_round_trip_validates_checksum():
    wire = sip2.frame("941", sequence=4)
    response = sip2.parse(wire)
    assert response.code == "94"
    assert response.ok
    assert response.sequence == 4
    assert response.checksum_ok is True


def test_parse_detects_bad_checksum():
    wire = sip2.frame("941", sequence=4)[:-1]
    tampered = wire[:-4] + "0000"
    assert sip2.parse(tampered).checksum_ok is False


def test_parse_without_trailer():
    response = sip2.parse("940\r")
    assert response.code == "94"
    assert response.ok is False
    assert response.sequence is None
    assert response.checksum_ok is None


def test_parse_98_fixed_fields_and_variable_fields():
    body = ("98YYYYNN015003" + "20261014    120000" + "2.00"
            "AOCERN|AMCERN Library|BXYYYYNNYYNNNNNNNY|")
    response = sip2.parse(sip2.frame(body, 0))
    assert response.code == "98"
    assert response.fixed["online_status"] == "Y"
    assert response.fixed["offline_ok"] == "N"
    assert response.fixed["timeout_period"] == "015"
    assert response.fixed["retries_allowed"] == "003"
    assert response.fixed["date_time_sync"] == "20261014    120000"
    assert response.fixed["protocol_version"] == "2.00"
    assert response.get("AO") == "CERN"
    assert response.get("AM") == "CERN Library"
    assert response.get("BX").startswith("YYYY")


def test_parse_64_counts_and_repeated_fields():
    body = ("64" + " " * 14 + "001" + "20261014    120000"
            + "0001" + "0001" + "0002" + "0000" + "0000" + "0000"
            + "AOCERN|AAP001|AEAda Lovelace|AUIT002|AUIT005|BLY|CQY|")
    response = sip2.parse(body)
    assert response.code == "64"
    assert response.fixed["charged_items_count"] == "0002"
    assert response.fixed["hold_items_count"] == "0001"
    assert response.all("AU") == ["IT002", "IT005"]
    assert response.get("BL") == "Y"
    assert response.get("CQ") == "Y"
    assert response.get("ZZ", "default") == "default"


def test_parse_12_checkout_response():
    body = "121NNY20261014    120000AOCERN|AAP001|ABIT001|AJSICP|AH20261111    235959|"
    response = sip2.parse(body)
    assert response.code == "12"
    assert response.ok
    assert response.fixed["renewal_ok"] == "N"
    assert response.fixed["desensitize"] == "Y"
    assert response.get("AH") == "20261111    235959"


def test_parse_unknown_code_keeps_everything_as_variable_fields():
    response = sip2.parse("XXAOCERN|")
    assert response.code == "XX"
    assert response.fixed == {}
    assert response.get("AO") == "CERN"


@pytest.mark.parametrize("builder, args, prefix", [
    (sip2.login, ("u", "p", "loc"), "9300CNu|COp|CPloc|"),
    (sip2.sc_status, (), "9900402.00"),
    (sip2.item_information, ("IT1", "CERN"), "17"),
    (sip2.end_patron_session, ("P1", "CERN"), "35"),
])
def test_builders_start_with_expected_prefix(builder, args, prefix):
    assert builder(*args).startswith(prefix)


def test_checkout_builder_layout():
    body = sip2.checkout("P1", "IT1", "CERN", pin="1234")
    assert body[:2] == "11"
    assert body[2] == "Y" and body[3] == "N"  # renewal policy, no block
    assert re.match(r"^\d{8} {4}\d{6}$", body[4:22])  # transaction date
    assert body[22:40] == " " * 18  # nb due date blank
    assert body[40:] == "AOCERN|AAP1|ABIT1|AC|AD1234|"


def test_patron_information_builder_layout():
    body = sip2.patron_information("P1", "CERN", pin="1234")
    assert body[:2] == "63"
    assert body[2:5] == "001"
    assert body[23:33] == " " * 10  # summary
    assert body[33:] == "AOCERN|AAP1|AD1234|"
