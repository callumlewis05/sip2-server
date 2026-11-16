"""Routing, login gating, 96/97, resend handling and transaction ids.

One call to :meth:`Dispatcher.dispatch` per received frame:

1. Decode. Garbage closes the connection; a bad checksum or truncated
   message gets a ``96``, which is never cached.
2. ``97`` replays the last response byte for byte.
3. A frame identical to the previous one (the kiosk resent after a lost
   reply) gets the cached response without a second ILS call, unless that
   response was an ambiguous refusal, in which case the ILS is asked again
   with the same transaction id.
4. Before login only ``93`` and ``99`` are routed; anything else gets a
   well-formed refusal. Known-but-unsupported messages get one too.
   Unknown codes get a ``96``.
"""

import hashlib
import logging
from dataclasses import replace
from typing import NamedTuple

from . import codec
from .messages import REPLY_CODE, REQUESTS

logger = logging.getLogger("cds_sip2.sip2.dispatcher")

RESEND = b"96\r"
"""Like 97, 96 carries no AY/AZ: the sequence number of a corrupted frame is
not trustworthy."""

PRE_LOGIN = frozenset({"93", "99"})


class Outcome(NamedTuple):
    reply: bytes | None
    close: bool = False


def transaction_id(request, terminal):
    """Deterministic id for a circulation request, sent to the ILS.

    A kiosk resending after a timeout or a ``96`` sends the same content, so
    it gets the same id, even over a new connection. The message code keeps
    a checkout and a later checkin of the same item apart, and the sequence
    number and the kiosk's transaction date keep two deliberate identical
    requests apart. PINs and passwords (AD, AC) are left out: the id is
    logged and a 4-digit PIN in a hash is easily recovered.
    """
    parts = [
        terminal or "",
        request.code,
        "" if request.sequence is None else str(request.sequence),
    ]
    parts += [request.fixed[name] for name, _ in REQUESTS[request.code].fixed]
    parts += [request.get(tag) for tag in ("AO", "AA", "AB")]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


class Dispatcher:
    def __init__(self, handlers, settings):
        self.handlers = handlers
        self.settings = settings

    def _encode(self, response, request):
        return codec.encode(
            response, request.sequence, self.settings.error_detection, self.settings.encoding
        )

    def _log(self, direction, frame, session, **extra):
        logger.info(
            direction,
            extra={
                "event": f"sip2.{direction}",
                "frame": codec.redact(frame, self.settings.encoding),
                "terminal": session.terminal,
                "peer": session.peer,
                **extra,
            },
        )

    async def dispatch(self, frame, session):
        self._log("request", frame, session)
        try:
            request = codec.decode(frame, self.settings.error_detection, self.settings.encoding)
        except codec.FrameError as exc:
            logger.warning(
                "not a SIP2 frame, closing connection",
                extra={
                    "event": "sip2.garbage",
                    "reason": str(exc),
                    "length": len(frame),
                    "peer": session.peer,
                },
            )
            return Outcome(None, close=True)
        except codec.ResendError as exc:
            logger.warning(
                "asking client to resend",
                extra={"event": "sip2.resend_request", "reason": str(exc), "peer": session.peer},
            )
            return self._reply(RESEND, session)

        if request.code == "97":
            if session.last_response is None:
                return self._reply(RESEND, session)
            logger.info(
                "replaying last response",
                extra={"event": "sip2.replay", "terminal": session.terminal},
            )
            return self._reply(session.last_response, session)

        if session.replayable and frame == session.last_request:
            logger.info(
                "identical resend, replaying cached response",
                extra={"event": "sip2.replay", "terminal": session.terminal},
            )
            return self._reply(session.last_response, session)

        if request.code not in REPLY_CODE:
            logger.warning(
                "unknown message code",
                extra={"event": "sip2.unknown", "code": request.code, "peer": session.peer},
            )
            return self._reply(RESEND, session)

        response = await self._route(request, session)
        reply = self._encode(response, request)
        session.remember(frame, reply, replayable=not response.ambiguous)
        close = request.code == "93" and session.failed_logins >= self.settings.max_login_attempts
        if close:
            logger.warning(
                "too many failed logins, closing connection",
                extra={"event": "login", "peer": session.peer},
            )
        return self._reply(reply, session, close)

    async def _route(self, request, session):
        handlers = self.handlers
        if request.code not in PRE_LOGIN and not session.authenticated:
            logger.warning(
                "request before login refused",
                extra={"event": "sip2.not_logged_in", "code": request.code, "peer": session.peer},
            )
            return handlers.refuse(request, session, "not_logged_in")
        route = handlers.routes.get(request.code)
        if route is None:
            logger.info(
                "unsupported message refused",
                extra={
                    "event": "sip2.unsupported",
                    "code": request.code,
                    "terminal": session.terminal,
                },
            )
            return handlers.refuse(request, session, "unsupported")
        request = replace(request, transaction_id=transaction_id(request, session.terminal))
        try:
            return await route(request, session)
        except Exception:
            logger.exception(
                "handler crashed",
                extra={
                    "event": "sip2.handler_error",
                    "code": request.code,
                    "alert": True,
                    "terminal": session.terminal,
                },
            )
            return handlers.refuse(request, session, "unavailable")

    def _reply(self, reply, session, close=False):
        self._log("response", reply, session)
        return Outcome(reply, close)
