"""Declarative layout of every SIP2 2.00 message.

A message is a two-digit code, a run of fixed-width fields, then ``|``-
terminated variable fields, each a two-letter tag and a value. For responses
``fields`` is the emission order and ``required`` lists the tags that are
always sent, empty if need be.
"""

from dataclasses import dataclass

DATE = 18  # YYYYMMDDZZZZHHMMSS


@dataclass(frozen=True)
class MessageSpec:
    code: str
    name: str
    fixed: tuple = ()
    fields: tuple = ()
    required: tuple = ()

    @property
    def fixed_width(self):
        return sum(width for _, width in self.fixed)


def _specs(*specs):
    return {spec.code: spec for spec in specs}


REQUESTS = _specs(
    MessageSpec("93", "Login", (("uid_algorithm", 1), ("pwd_algorithm", 1)), ("CN", "CO", "CP")),
    MessageSpec(
        "99", "SC Status", (("status_code", 1), ("max_print_width", 3), ("protocol_version", 4))
    ),
    MessageSpec("97", "Request ACS Resend"),
    MessageSpec(
        "23",
        "Patron Status Request",
        (("language", 3), ("transaction_date", DATE)),
        ("AO", "AA", "AC", "AD"),
    ),
    MessageSpec("25", "Patron Enable", (("transaction_date", DATE),), ("AO", "AA", "AC", "AD")),
    MessageSpec(
        "63",
        "Patron Information",
        (("language", 3), ("transaction_date", DATE), ("summary", 10)),
        ("AO", "AA", "AC", "AD", "BP", "BQ"),
    ),
    MessageSpec("17", "Item Information", (("transaction_date", DATE),), ("AO", "AB", "AC")),
    MessageSpec(
        "11",
        "Checkout",
        (
            ("sc_renewal_policy", 1),
            ("no_block", 1),
            ("transaction_date", DATE),
            ("nb_due_date", DATE),
        ),
        ("AO", "AA", "AB", "AC", "CH", "AD", "BO", "BI"),
    ),
    MessageSpec(
        "09",
        "Checkin",
        (("no_block", 1), ("transaction_date", DATE), ("return_date", DATE)),
        ("AP", "AO", "AB", "AC", "CH", "BI"),
    ),
    MessageSpec(
        "29",
        "Renew",
        (
            ("third_party_allowed", 1),
            ("no_block", 1),
            ("transaction_date", DATE),
            ("nb_due_date", DATE),
        ),
        ("AO", "AA", "AD", "AB", "AJ", "AC", "CH", "BO"),
    ),
    MessageSpec(
        "35", "End Patron Session", (("transaction_date", DATE),), ("AO", "AA", "AC", "AD")
    ),
    # Known but not supported: answered with a well-formed refusal.
    MessageSpec(
        "01",
        "Block Patron",
        (("card_retained", 1), ("transaction_date", DATE)),
        ("AO", "AL", "AA", "AC"),
    ),
    MessageSpec(
        "15",
        "Hold",
        (("hold_mode", 1), ("transaction_date", DATE)),
        ("BW", "BS", "BY", "AO", "AA", "AD", "AB", "AJ", "AC", "BO"),
    ),
    MessageSpec(
        "19", "Item Status Update", (("transaction_date", DATE),), ("AO", "AB", "AC", "CH")
    ),
    MessageSpec(
        "37",
        "Fee Paid",
        (("transaction_date", DATE), ("fee_type", 2), ("payment_type", 2), ("currency_type", 3)),
        ("BV", "AO", "AA", "AC", "AD", "CG", "BK"),
    ),
    MessageSpec("65", "Renew All", (("transaction_date", DATE),), ("AO", "AA", "AD", "AC", "BO")),
)

_PATRON_FIXED = (("patron_status", 14), ("language", 3), ("transaction_date", DATE))
_CIRCULATION_FIXED = (
    ("ok", 1),
    ("renewal_ok", 1),
    ("magnetic_media", 1),
    ("desensitize", 1),
    ("transaction_date", DATE),
)

RESPONSES = _specs(
    MessageSpec("94", "Login Response", (("ok", 1),)),
    MessageSpec("96", "Request SC Resend"),
    MessageSpec(
        "98",
        "ACS Status",
        (
            ("online_status", 1),
            ("checkin_ok", 1),
            ("checkout_ok", 1),
            ("acs_renewal_policy", 1),
            ("status_update_ok", 1),
            ("offline_ok", 1),
            ("timeout_period", 3),
            ("retries_allowed", 3),
            ("date_time_sync", DATE),
            ("protocol_version", 4),
        ),
        ("AO", "AM", "BX", "AN", "AF", "AG"),
        ("AO", "BX"),
    ),
    MessageSpec(
        "24",
        "Patron Status Response",
        _PATRON_FIXED,
        ("AO", "AA", "AE", "BL", "CQ", "BH", "BV", "AF", "AG"),
        ("AO", "AA", "AE"),
    ),
    MessageSpec(
        "26",
        "Patron Enable Response",
        _PATRON_FIXED,
        ("AO", "AA", "AE", "BL", "CQ", "AF", "AG"),
        ("AO", "AA", "AE"),
    ),
    MessageSpec(
        "64",
        "Patron Information Response",
        _PATRON_FIXED
        + (
            ("hold_items_count", 4),
            ("overdue_items_count", 4),
            ("charged_items_count", 4),
            ("fine_items_count", 4),
            ("recall_items_count", 4),
            ("unavailable_holds_count", 4),
        ),
        (
            "AO",
            "AA",
            "AE",
            "BZ",
            "CA",
            "CB",
            "BL",
            "CQ",
            "BH",
            "BV",
            "CC",
            "AS",
            "AT",
            "AU",
            "AV",
            "BU",
            "CD",
            "BD",
            "BE",
            "BF",
            "AF",
            "AG",
        ),
        ("AO", "AA", "AE"),
    ),
    MessageSpec(
        "18",
        "Item Information Response",
        (
            ("circulation_status", 2),
            ("security_marker", 2),
            ("fee_type", 2),
            ("transaction_date", DATE),
        ),
        ("CF", "AH", "CJ", "CM", "AB", "AJ", "BG", "BH", "BV", "CK", "AQ", "AP", "CH", "AF", "AG"),
        ("AB", "AJ"),
    ),
    MessageSpec(
        "12",
        "Checkout Response",
        _CIRCULATION_FIXED,
        ("AO", "AA", "AB", "AJ", "AH", "BT", "CI", "BH", "BV", "CK", "CH", "AF", "AG"),
        ("AO", "AA", "AB", "AJ", "AH"),
    ),
    MessageSpec(
        "10",
        "Checkin Response",
        (
            ("ok", 1),
            ("resensitize", 1),
            ("magnetic_media", 1),
            ("alert", 1),
            ("transaction_date", DATE),
        ),
        ("AO", "AB", "AQ", "AJ", "CL", "CK", "CH", "AF", "AG"),
        ("AO", "AB", "AQ"),
    ),
    MessageSpec(
        "30",
        "Renew Response",
        _CIRCULATION_FIXED,
        ("AO", "AA", "AB", "AJ", "AH", "BT", "CI", "BH", "BV", "CK", "CH", "AF", "AG"),
        ("AO", "AA", "AB", "AJ", "AH"),
    ),
    MessageSpec(
        "36",
        "End Session Response",
        (("end_session", 1), ("transaction_date", DATE)),
        ("AO", "AA", "AF", "AG"),
        ("AO", "AA"),
    ),
    MessageSpec(
        "16",
        "Hold Response",
        (("ok", 1), ("available", 1), ("transaction_date", DATE)),
        ("BW", "BR", "BS", "AO", "AA", "AB", "AJ", "AF", "AG"),
        ("AO", "AA"),
    ),
    MessageSpec(
        "20",
        "Item Status Update Response",
        (("item_properties_ok", 1), ("transaction_date", DATE)),
        ("AB", "AJ", "CH", "AF", "AG"),
        ("AB",),
    ),
    MessageSpec(
        "38",
        "Fee Paid Response",
        (("payment_accepted", 1), ("transaction_date", DATE)),
        ("AO", "AA", "BK", "AF", "AG"),
        ("AO", "AA"),
    ),
    MessageSpec(
        "66",
        "Renew All Response",
        (("ok", 1), ("renewed_count", 4), ("unrenewed_count", 4), ("transaction_date", DATE)),
        ("AO", "BM", "BN", "AF", "AG"),
        ("AO",),
    ),
)

REPLY_CODE = {
    "93": "94",
    "99": "98",
    "23": "24",
    "25": "26",
    "63": "64",
    "17": "18",
    "11": "12",
    "09": "10",
    "29": "30",
    "35": "36",
    "01": "24",
    "15": "16",
    "19": "20",
    "37": "38",
    "65": "66",
}

SUPPORTED_MESSAGES_ORDER = (
    "23",
    "11",
    "09",
    "01",
    "99",
    "97",
    "93",
    "63",
    "35",
    "37",
    "17",
    "19",
    "25",
    "15",
    "29",
    "65",
)
"""Position of each request code in the 98's ``BX`` supported-messages field."""

SUMMARY_LISTS = ("AS", "AT", "AU", "AV", "BU", "CD")
"""63 ``summary`` position -> list tag in the 64: hold, overdue, charged, fine,
recall, unavailable hold items."""


def supported_messages(codes):
    """``BX`` value for a set of supported request codes."""
    return "".join("Y" if code in codes else "N" for code in SUPPORTED_MESSAGES_ORDER)
