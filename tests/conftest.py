"""Shared fixtures.

* ``fake_ils``: an in-memory :class:`~cds_sip2.domain.ILSBackend`
  for the SIP2-side tests (handlers, dispatcher, server). It speaks Python,
  not HTTP, exactly like the seam between the packages.
* ``router``: an ``httpx.MockTransport`` handler for the REST client tests.

Async code is driven with ``asyncio.run`` so no pytest plugin is needed.
"""

import asyncio
import inspect
import json as jsonlib
from datetime import date

import httpx
import pytest

from cds_sip2.config import Settings
from cds_sip2.domain import CheckinResult, CheckoutResult, ILSBackend, Item, ItemStatus
from cds_sip2.sip2.codec import decode
from cds_sip2.sip2.dispatcher import Dispatcher
from cds_sip2.sip2.handlers import Handlers
from cds_sip2.sip2.session import Session

ILS_URL = "http://ils.test/api/sip2"


def run(coro):
    return asyncio.run(coro)


ITEM = Item(
    barcode="IT001",
    title="SICP",
    status=ItemStatus.AVAILABLE,
    location="Main reading room",
    hold_queue_length=2,
    owner="CERN Library",
)
CHECKOUT_RESULT = CheckoutResult(title="SICP", due_date=date(2026, 11, 11))
CHECKIN_RESULT = CheckinResult(title="SICP", permanent_location="Main reading room")
TERMINALS = {"kiosk1": {"password": "secret", "user_pid": "42"}}


class FakeILS:
    """In-memory :class:`~cds_sip2.domain.ILSBackend`.

    Records every call; answers from ``self.answers[name]``. An answer may
    be a value, an exception (raised), or a callable taking the call's
    keyword arguments.
    """

    def __init__(self):
        self.calls = []
        self.answers = {
            "ping": True,
            "get_item": ITEM,
            "checkout": CHECKOUT_RESULT,
            "checkin": CHECKIN_RESULT,
            "update_item_status": ITEM,
        }

    def calls_to(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    async def _answer(self, name, **kwargs):
        self.calls.append((name, kwargs))
        answer = self.answers[name]
        if isinstance(answer, BaseException) or (
            inspect.isclass(answer) and issubclass(answer, BaseException)
        ):
            raise answer
        if callable(answer):
            answer = answer(**kwargs)
            if inspect.isawaitable(answer):
                answer = await answer
        return answer

    async def ping(self):
        return await self._answer("ping")

    async def get_item(self, barcode):
        return await self._answer("get_item", barcode=barcode)

    async def checkout(self, *, item_barcode, patron_barcode, caller):
        return await self._answer(
            "checkout", item_barcode=item_barcode, patron_barcode=patron_barcode, caller=caller
        )

    async def checkin(self, *, item_barcode, caller):
        return await self._answer("checkin", item_barcode=item_barcode, caller=caller)

    async def update_item_status(self, *, item_barcode, properties, caller):
        return await self._answer(
            "update_item_status", item_barcode=item_barcode, properties=properties, caller=caller
        )

    async def aclose(self):
        pass


@pytest.fixture
def settings():
    return Settings(
        ils_url=ILS_URL,
        ils_token="t",
        port=0,
        health_port=0,
        log_hash_key="test-key",
        terminals=TERMINALS,
    )


@pytest.fixture
def fake_ils():
    backend: ILSBackend = FakeILS()  # checked by a type checker, if one runs
    return backend


@pytest.fixture
def handlers(fake_ils, settings):
    return Handlers(fake_ils, settings)


@pytest.fixture
def dispatcher(handlers, settings):
    return Dispatcher(handlers, settings)


@pytest.fixture
def session():
    """A session that has already logged in as kiosk1."""
    s = Session(peer="127.0.0.1:5000")
    s.login("kiosk1", "42", "CERN", "CERN Library", "en")
    return s


def request(body):
    """Decode a request body (no trailer), as the dispatcher would see it."""
    return decode(body.encode(), error_detection=True)


class Router:
    """``httpx.MockTransport`` handler: register ``(method, path)`` answers."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, path, status=200, json=None, content=None, exc=None):
        self.routes[(method, path)] = (status, json, content, exc)

    def get(self, path, **kwargs):
        self.add("GET", path, **kwargs)

    def post(self, path, **kwargs):
        self.add("POST", path, **kwargs)

    def body(self, index=0):
        return jsonlib.loads(self.calls[index].content)

    def __call__(self, req):
        self.calls.append(req)
        path = req.url.raw_path.decode().split("?")[0].removeprefix("/api/sip2")
        status, json, content, exc = self.routes[(req.method, path)]
        if exc is not None:
            raise exc(f"simulated {exc.__name__}", request=req)
        if json is not None:
            return httpx.Response(status, json=json)
        return httpx.Response(status, content=content or b"")


@pytest.fixture
def router():
    return Router()
