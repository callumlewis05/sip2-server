"""Typed views of SIP2 messages: the layer that keeps tags out of the handlers.

* A **request class** names the fields a handler reads. ``parse`` fills it
  from a decoded :class:`~cds_sip2.sip2.codec.Request`.
* A **response class** names the fields a handler sets. Its defaults are
  the message's *refused* state, so :func:`refuse` can answer any known
  request with a well-formed negative reply, and a new message needs a class
  rather than a hand-written refusal. ``to_response`` turns it into a wire
  :class:`~cds_sip2.sip2.codec.Response`.

Each field declares its SIP2 tag or fixed-field name. The ``@request`` and
``@response`` decorators check those against :mod:`.messages` at import
time, so a misspelt tag fails at start-up (and in every test run), not as a
"handler crashed" refusal in front of a patron. A misspelt *attribute*
fails at construction, as with any dataclass.
"""

from dataclasses import dataclass, field, fields
from typing import ClassVar, NamedTuple

from ..utils import normalize_barcode, sip2_now
from . import enums
from .codec import Request, Response, build
from .messages import REPLY_CODE, REQUESTS, RESPONSES


class Reply(NamedTuple):
    """What a handler returns to the dispatcher."""

    response: Response
    replayable: bool = True
    """False when the ILS outcome is unknown: an identical resend must ask the
    ILS again rather than get this reply from the cache."""


# ------------------------------------------------------------ field helpers

_TEXT, _YN, _BIT = "text", "yn", "bit"


def tag(code, default=None, *, barcode=False):
    """A variable field. ``None`` is not sent; a tuple becomes repeated fields."""
    return field(default=default, metadata={"tag": code, "barcode": barcode})


def fixed(name, default="", kind=_TEXT):
    return field(default=default, metadata={"fixed": name, "kind": kind})


def yes_no(name, default=False):
    """A boolean fixed field sent as ``Y`` / ``N``."""
    return fixed(name, default, _YN)


def bit(name, default=False):
    """A boolean fixed field sent as ``1`` / ``0``."""
    return fixed(name, default, _BIT)


def _now(name):
    return field(default_factory=sip2_now, metadata={"fixed": name, "kind": _TEXT})


def _encode_fixed(value, kind):
    if kind == _YN:
        return "Y" if value else "N"
    if kind == _BIT:
        return "1" if value else "0"
    return str(value)


def _encode_tag(value):
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return str(value)


def _check(cls, spec, kind):
    names = {name for name, _ in spec.fixed}
    for f in fields(cls):
        if "tag" in f.metadata and f.metadata["tag"] not in spec.fields:
            raise TypeError(f"{cls.__name__}.{f.name}: {f.metadata['tag']} is not a {kind} tag")
        if "fixed" in f.metadata and f.metadata["fixed"] not in names:
            raise TypeError(f"{cls.__name__}.{f.name}: no fixed field {f.metadata['fixed']!r}")
    return names


# ----------------------------------------------------------------- requests

REQUEST_TYPES = {}


class _RequestBase:
    CODE: ClassVar[str]

    @classmethod
    def parse(cls, request):
        values = {"source": request}
        for f in fields(cls):
            if "tag" in f.metadata:
                value = request.get(f.metadata["tag"])
                values[f.name] = normalize_barcode(value) if f.metadata["barcode"] else value
            elif "fixed" in f.metadata:
                values[f.name] = request.fixed.get(f.metadata["fixed"], "")
        return cls(**values)


def request(code):
    def wrap(cls):
        cls = dataclass(frozen=True, slots=True)(cls)
        _check(cls, REQUESTS[code], "request")
        cls.CODE = code
        REQUEST_TYPES[code] = cls
        return cls

    return wrap


def parse(req):
    """Typed view of ``req``, or ``None`` if its code has no request class."""
    cls = REQUEST_TYPES.get(req.code)
    return cls.parse(req) if cls else None


@request("93")
class LoginRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    terminal: str = tag("CN", "")
    password: str = field(default="", repr=False, metadata={"tag": "CO", "barcode": False})
    location: str = tag("CP", "")


@request("99")
class SCStatusRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    status_code: str = fixed("status_code")


@request("17")
class ItemInformationRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    institution_id: str = tag("AO", "")
    item_barcode: str = tag("AB", "", barcode=True)


@request("11")
class CheckoutRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "", barcode=True)
    item_barcode: str = tag("AB", "", barcode=True)


@request("09")
class CheckinRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    institution_id: str = tag("AO", "")
    item_barcode: str = tag("AB", "", barcode=True)
    current_location: str = tag("AP", "")


@request("19")
class ItemStatusUpdateRequest(_RequestBase):
    source: Request | None = field(default=None, repr=False, compare=False)
    institution_id: str = tag("AO", "")
    item_barcode: str = tag("AB", "", barcode=True)
    properties: str = tag("CH", "")


# ---------------------------------------------------------------- responses

RESPONSE_TYPES = {}


class _ResponseBase:
    CODE: ClassVar[str]

    def to_response(self):
        fixed_values, tags = {}, {}
        for f in fields(self):
            value = getattr(self, f.name)
            if "fixed" in f.metadata:
                fixed_values[f.metadata["fixed"]] = _encode_fixed(value, f.metadata["kind"])
            elif "tag" in f.metadata and value is not None:
                tags[f.metadata["tag"]] = _encode_tag(value)
        return build(self.CODE, fixed_values, **tags)

    def reply(self, replayable=True):
        return Reply(self.to_response(), replayable)


def response(code):
    def wrap(cls):
        cls = dataclass(frozen=True, slots=True)(cls)
        spec = RESPONSES[code]
        names = _check(cls, spec, "response")
        declared = {f.metadata["fixed"] for f in fields(cls) if "fixed" in f.metadata}
        if declared != names:
            raise TypeError(f"{cls.__name__}: fixed fields {sorted(declared)} != {sorted(names)}")
        cls.CODE = code
        RESPONSE_TYPES[code] = cls
        return cls

    return wrap


@response("94")
class LoginResponse(_ResponseBase):
    ok: bool = bit("ok")


@response("98")
class ACSStatus(_ResponseBase):
    online_status: bool = yes_no("online_status")
    checkin_ok: bool = yes_no("checkin_ok")
    checkout_ok: bool = yes_no("checkout_ok")
    acs_renewal_policy: bool = yes_no("acs_renewal_policy")
    status_update_ok: bool = yes_no("status_update_ok")
    offline_ok: bool = yes_no("offline_ok")
    timeout_period: str = fixed("timeout_period", "000")
    retries_allowed: str = fixed("retries_allowed", "000")
    date_time_sync: str = _now("date_time_sync")
    protocol_version: str = fixed("protocol_version", "2.00")
    institution_id: str = tag("AO", "")
    library_name: str | None = tag("AM")
    supported_messages: str = tag("BX", "")
    screen_message: str | None = tag("AF")


@response("18")
class ItemInformationResponse(_ResponseBase):
    circulation_status: str = fixed("circulation_status", enums.CirculationStatus.OTHER)
    security_marker: str = fixed("security_marker", enums.SecurityMarker.OTHER)
    fee_type: str = fixed("fee_type", enums.FeeType.OTHER)
    transaction_date: str = _now("transaction_date")
    hold_queue_length: int | None = tag("CF")
    due_date: str | None = tag("AH")
    item_barcode: str = tag("AB", "")
    title: str = tag("AJ", "")
    owner: str | None = tag("BG")
    media_type: str | None = tag("CK")
    permanent_location: str | None = tag("AQ")
    current_location: str | None = tag("AP")
    screen_message: str | tuple | None = tag("AF")


@response("12")
class CheckoutResponse(_ResponseBase):
    ok: bool = bit("ok")
    renewal_ok: bool = yes_no("renewal_ok")
    magnetic_media: bool = yes_no("magnetic_media")
    desensitize: bool = yes_no("desensitize")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    item_barcode: str = tag("AB", "")
    title: str = tag("AJ", "")
    due_date: str = tag("AH", "")
    media_type: str | None = tag("CK")
    screen_message: str | tuple | None = tag("AF")


@response("10")
class CheckinResponse(_ResponseBase):
    ok: bool = bit("ok")
    resensitize: bool = yes_no("resensitize")
    magnetic_media: bool = yes_no("magnetic_media")
    alert: bool = yes_no("alert", True)
    """Refused checkins beep, so the item goes to the desk."""
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    item_barcode: str = tag("AB", "")
    permanent_location: str = tag("AQ", "")
    title: str = tag("AJ", "")
    media_type: str | None = tag("CK")
    screen_message: str | tuple | None = tag("AF")


@response("20")
class ItemStatusUpdateResponse(_ResponseBase):
    item_properties_ok: bool = bit("item_properties_ok")
    transaction_date: str = _now("transaction_date")
    item_barcode: str = tag("AB", "")
    title: str | None = tag("AJ")
    screen_message: str | tuple | None = tag("AF")


# Responses to messages that are not supported. They exist only so that
# :func:`refuse` can answer those requests; their defaults are the refusal.


@response("24")
class PatronStatusResponse(_ResponseBase):
    patron_status: str = fixed("patron_status", enums.DENIED_STATUS)
    language: str = fixed("language", "000")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    valid_patron: bool = tag("BL", False)
    screen_message: str | None = tag("AF")


@response("26")
class PatronEnableResponse(_ResponseBase):
    patron_status: str = fixed("patron_status", enums.DENIED_STATUS)
    language: str = fixed("language", "000")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    valid_patron: bool = tag("BL", False)
    screen_message: str | None = tag("AF")


@response("64")
class PatronInformationResponse(_ResponseBase):
    patron_status: str = fixed("patron_status", enums.DENIED_STATUS)
    language: str = fixed("language", "000")
    transaction_date: str = _now("transaction_date")
    hold_items_count: str = fixed("hold_items_count", "0000")
    overdue_items_count: str = fixed("overdue_items_count", "0000")
    charged_items_count: str = fixed("charged_items_count", "0000")
    fine_items_count: str = fixed("fine_items_count", "0000")
    recall_items_count: str = fixed("recall_items_count", "0000")
    unavailable_holds_count: str = fixed("unavailable_holds_count", "0000")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    valid_patron: bool = tag("BL", False)
    screen_message: str | None = tag("AF")


@response("30")
class RenewResponse(_ResponseBase):
    ok: bool = bit("ok")
    renewal_ok: bool = yes_no("renewal_ok")
    magnetic_media: bool = yes_no("magnetic_media")
    desensitize: bool = yes_no("desensitize")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    item_barcode: str = tag("AB", "")
    title: str = tag("AJ", "")
    due_date: str = tag("AH", "")
    screen_message: str | None = tag("AF")


@response("36")
class EndSessionResponse(_ResponseBase):
    end_session: bool = yes_no("end_session")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    screen_message: str | None = tag("AF")


@response("16")
class HoldResponse(_ResponseBase):
    ok: bool = bit("ok")
    available: bool = yes_no("available")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    screen_message: str | None = tag("AF")


@response("38")
class FeePaidResponse(_ResponseBase):
    payment_accepted: bool = yes_no("payment_accepted")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    patron_barcode: str = tag("AA", "")
    screen_message: str | None = tag("AF")


@response("66")
class RenewAllResponse(_ResponseBase):
    ok: bool = bit("ok")
    renewed_count: str = fixed("renewed_count", "0000")
    unrenewed_count: str = fixed("unrenewed_count", "0000")
    transaction_date: str = _now("transaction_date")
    institution_id: str = tag("AO", "")
    screen_message: str | None = tag("AF")


# ------------------------------------------------------------------ refusal

_ECHOED = ("AA", "AB")
"""Request tags a refusal repeats back, so the device can match it up."""


def refuse(req, *, institution_id, language, message, **overrides):
    """The refused-state response class for ``req``, filled from context.

    ``overrides`` are attribute names of the response class (e.g. ``title``).
    """
    cls = RESPONSE_TYPES[REPLY_CODE[req.code]]
    values = {}
    for f in fields(cls):
        meta = f.metadata
        if meta.get("tag") == "AO":
            values[f.name] = institution_id
        elif meta.get("tag") == "AF":
            values[f.name] = message
        elif meta.get("tag") in _ECHOED:
            values[f.name] = normalize_barcode(req.get(meta["tag"]))
        elif meta.get("fixed") == "language":
            values[f.name] = language
    values.update(overrides)
    return cls(**values)
