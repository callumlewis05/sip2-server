"""Shared fixtures.

Unit tests run against a bare ``Flask`` app with ``InvenioSIP2`` and
``CDSSip2`` initialised by hand; Redis is replaced by fakeredis and the ILS by
the ``responses`` library.  Nothing here spawns the socket server.
"""

import fakeredis
import pytest
import responses as responses_lib
from flask import Flask

from cds_sip2.ext import CDSSip2

ILS_URL = "http://ils.test/api/sip2"
ILS_TOKEN = "unit-test-token"


def url(path):
    """Absolute URL of an ILS endpoint, for registering ``responses``."""
    return f"{ILS_URL}/{path.lstrip('/')}"


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Route every ``redis.*.from_url`` to one in-memory fakeredis server."""
    import redis

    server = fakeredis.FakeServer()

    def from_url(cls, url, **kwargs):  # noqa: ARG001 - signature mirrors redis
        return fakeredis.FakeStrictRedis(server=server)

    monkeypatch.setattr(redis.StrictRedis, "from_url", classmethod(from_url))
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(from_url))
    return server


@pytest.fixture
def app(tmp_path):
    app = Flask("cds_sip2_tests", instance_path=str(tmp_path))
    app.config.update(
        TESTING=True,
        SECRET_KEY="test",
        CDS_SIP2_ILS_URL=ILS_URL,
        CDS_SIP2_ILS_TOKEN=ILS_TOKEN,
        CDS_SIP2_ILS_CONNECT_TIMEOUT=1.0,
        CDS_SIP2_ILS_READ_TIMEOUT=1.0,
        SIP2_DATASTORE_REDIS_URL="redis://fake:6379/0",
        SIP2_LOGGING_CONSOLE=False,
        SIP2_LOGGING_FS_LOGFILE=None,
    )
    try:  # invenio-sip2 uses the i18n helpers; initialise them when available
        from invenio_i18n import InvenioI18N

        InvenioI18N(app)
    except ImportError:
        pass
    from invenio_sip2 import InvenioSIP2

    InvenioSIP2(app)
    CDSSip2(app)
    with app.app_context():
        yield app


@pytest.fixture
def ils(app):
    """``responses`` mock for the ILS; register the endpoints each test needs."""
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock
