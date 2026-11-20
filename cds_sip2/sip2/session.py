"""Per-connection state.

A session lives exactly as long as its TCP connection and is never shared
or persisted: after a reconnect the kiosk must send 93 Login again. That is
what makes Redis unnecessary. Idempotency across reconnects does not rely on
this state; see :func:`cds_sip2.sip2.dispatcher.transaction_id`.
"""

from dataclasses import dataclass

from ..domain import Caller


@dataclass
class Session:
    peer: str = ""
    """Address the connection came from (stunnel's, i.e. loopback, in the pod)."""

    # Set by a successful 93; terminal and user_pid go into every Caller.
    terminal: str | None = None
    user_pid: str | None = None
    institution_id: str | None = None
    library_name: str | None = None
    language: str | None = None

    failed_logins: int = 0

    # Last exchange, for 97 and for identical resends.
    last_request: bytes | None = None
    last_response: bytes | None = None
    replayable: bool = False
    """``last_response`` may answer an identical resend without asking the ILS."""

    @property
    def authenticated(self):
        return self.user_pid is not None

    def caller(self, transaction_id):
        """The :class:`~cds_sip2.domain.Caller` for a request on this session."""
        if not self.authenticated:
            raise RuntimeError("no Caller before login")
        return Caller(self.terminal, self.user_pid, transaction_id)

    def login(self, terminal, user_pid, institution_id, library_name, language):
        self.terminal = terminal
        self.user_pid = user_pid
        self.institution_id = institution_id
        self.library_name = library_name
        self.language = language
        self.failed_logins = 0

    def logout(self):
        self.terminal = self.user_pid = None
        self.institution_id = self.library_name = self.language = None

    def remember(self, request, response, replayable):
        self.last_request = request
        self.last_response = response
        self.replayable = replayable
