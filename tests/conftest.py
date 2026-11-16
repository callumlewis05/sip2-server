"""Shared fixtures.

* ``fake_ils``: an in-memory :class:`~cds_sip2.ils.interface.ILSBackend`
  for the SIP2-side tests (handlers, dispatcher, server). It speaks Python,
  not HTTP, exactly like the seam between the packages.
* ``router``: an ``httpx.MockTransport`` handler for the ILS client tests.

Async code is driven with ``asyncio.run`` so no pytest plugin is needed.
"""

import asyncio
import inspect
import json as jsonlib

import httpx
import pytest

from cds_sip2.config import Settings
from cds_sip2.sip2.codec import decode
from cds_sip2.sip2.dispatcher import Dispatcher
from cds_sip2.sip2.handlers import Handlers
from cds_sip2.sip2.session import Session

ILS_URL = "http://ils.test/api/sip2"


def run(coro):
    return asyncio.run(coro)


PATRON = {
    "barcode": "P001",
    "name": "Ada Lovelace",
    "email": "ada@example.org",
    "status_flags": [],
    "loans": ["IT002", "IT005"],
    "overdue": ["IT005"],
    "holds": ["IT009"],
    "recalls": [],
    "unavailable_holds": [],
    "fines": [],
    "fines_total": "0.00",
    "currency": "CHF",
}
ITEM = {
    "barcode": "IT001",
    "title": "SICP",
    "status": "available",
    "location": "Main reading room",
    "current_location": None,
    "due_date": None,
    "hold_queue_length": 2,
    "owner": "CERN Library",
}
LOGIN = {
    "ok": True,
    "user_pid": "42",
    "location_pid": "1",
    "institution_id": "CERN",
    "library_name": "CERN Library",
    "language": "en",
}


class FakeILS:
    """Records every call; answers from ``self.answers[name]``.

    An answer may be a value, an exception (raised), or a callable taking
    the call's keyword arguments.
    """

    def __init__(self):
        self.calls = []
        self.answers = {
            "ping": True,
            "login": LOGIN,
            "get_patron": PATRON,
            "verify_patron_pin": True,
            "get_item": ITEM,
            "checkout": {"title": "SICP", "due_date": "2026-11-11", "is_renewal": False},
            "checkin": {"title": "SICP", "permanent_location": "Main reading room", "alert": False},
            "renew": {"title": "SICP", "due_date": "2026-12-01"},
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

    async def login(self, terminal, password, ip=None):
        return await self._answer("login", terminal=terminal, password=password, ip=ip)

    async def get_patron(self, barcode):
        return await self._answer("get_patron", barcode=barcode)

    async def verify_patron_pin(self, barcode, pin):
        return await self._answer("verify_patron_pin", barcode=barcode, pin=pin)

    async def get_item(self, barcode):
        return await self._answer("get_item", barcode=barcode)

    async def checkout(self, **kwargs):
        return await self._answer("checkout", **kwargs)

    async def checkin(self, **kwargs):
        return await self._answer("checkin", **kwargs)

    async def renew(self, **kwargs):
        return await self._answer("renew", **kwargs)

    async def aclose(self):
        pass


@pytest.fixture
def settings():
    return Settings(ils_url=ILS_URL, ils_token="t", port=0, health_port=0, log_hash_key="test-key")


@pytest.fixture
def fake_ils():
    return FakeILS()


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
