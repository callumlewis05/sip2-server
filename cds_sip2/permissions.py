"""Permission factory for invenio-sip2's monitoring REST views.

``SIP2_PERMISSIONS_FACTORY`` guards the HTTP views that list servers and
client sessions. It has nothing to do with the socket server itself.  The
sidecar normally serves no HTTP, so the default behaviour is "deny
everything", which also keeps invenio-access's database-backed ``Permission``
out of the picture.  If ``CDS_SIP2_MONITORING_TOKEN`` is set, requests that
present it in the ``X-SIP2-Monitoring-Token`` header are allowed.
"""

import secrets
from contextlib import contextmanager

from flask import abort, current_app, has_request_context, request

HEADER = "X-SIP2-Monitoring-Token"


class StaticPermission:
    """Minimal object with the surface invenio-sip2 expects from a permission."""

    def __init__(self, allowed):
        self._allowed = bool(allowed)

    def can(self):
        return self._allowed

    def allows(self, identity=None):
        return self._allowed

    @contextmanager
    def require(self, http_exception=403):
        if not self._allowed:
            abort(http_exception)
        yield self

    def __bool__(self):
        return self._allowed


def permission_factory(action):
    """Return a permission object for ``action`` (the action is irrelevant here)."""
    expected = current_app.config.get("CDS_SIP2_MONITORING_TOKEN")
    if not expected or not has_request_context():
        return StaticPermission(False)
    provided = request.headers.get(HEADER, "")
    return StaticPermission(secrets.compare_digest(provided, expected))
