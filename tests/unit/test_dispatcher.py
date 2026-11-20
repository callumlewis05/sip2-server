"""Dispatcher: 96/97, login gating, resends, transaction ids, deadline, log safety."""

import asyncio
import io
import logging
from dataclasses import replace

import pytest

from cds_sip2 import logging as log
from cds_sip2.errors import ILSAmbiguous
from cds_sip2.sip2.dispatcher import RESEND, transaction_id
from cds_sip2.sip2.session import Session
from tests import sip2_client
from tests.conftest import CHECKOUT_RESULT, request, run

NOW = "20261102    120000"
BLANK = " " * 18
CHECKOUT = f"11YN{NOW}{BLANK}AOCERN|AAP001|ABIT001|AC|AD1234|"


def frame(body, sequence=0):
    return sip2_client.frame(body, sequence).rstrip("\r").encode()


def send(dispatcher, session, body, sequence=0):
    outcome = run(dispatcher.dispatch(frame(body, sequence), session))
    return outcome, sip2_client.parse(outcome.reply.decode()) if outcome.reply else None


def login(dispatcher, session):
    _, response = send(dispatcher, session, "9300CNkiosk1|COsecret|CP|")
    assert response.ok
    return response


# ----------------------------------------------------------------- framing


def test_response_echoes_sequence_with_valid_checksum(dispatcher, session):
    _, response = send(dispatcher, session, "9900402.00", sequence=7)
    assert response.code == "98"
    assert response.sequence == 7
    assert response.checksum_ok is True


def test_bad_checksum_gets_96_and_is_not_cached(dispatcher, session):
    send(dispatcher, session, "9900402.00", sequence=1)
    cached = session.last_response

    tampered = frame("9900402.00", 2)[:-4] + b"0000"
    outcome = run(dispatcher.dispatch(tampered, session))

    assert outcome.reply == RESEND
    assert not outcome.close
    assert session.last_response == cached  # a 97 now still replays the 98


def test_garbage_closes_the_connection(dispatcher, session):
    outcome = run(dispatcher.dispatch(b"\x16\x03\x01 TLS hello", session))
    assert outcome.reply is None and outcome.close


def test_unknown_code_gets_96(dispatcher, session):
    outcome, _ = send(dispatcher, session, "42hello|")
    assert outcome.reply == RESEND


# --------------------------------------------------------------------- 97


def test_97_replays_last_response_byte_for_byte(dispatcher, session):
    first, _ = send(dispatcher, session, "9900402.00", sequence=3)
    resent = run(dispatcher.dispatch(b"97", session))
    assert resent.reply == first.reply


def test_97_with_nothing_to_replay_gets_96(dispatcher):
    assert run(dispatcher.dispatch(b"97", Session())).reply == RESEND


# --------------------------------------------------------------- login gate


@pytest.mark.parametrize(
    "body, code",
    [
        (f"63001{NOW}{' ' * 10}AOCERN|AAP001|", "64"),
        (CHECKOUT, "12"),
        (f"17{NOW}AOCERN|ABIT001|", "18"),
        (f"35{NOW}AOCERN|AAP001|", "36"),
    ],
)
def test_only_93_and_99_before_login(dispatcher, fake_ils, body, code):
    _, response = send(dispatcher, Session(), body)

    assert response.code == code  # a well-formed refusal, not a dropped connection
    assert response.get("AE", "") == ""
    assert response.fixed.get("ok", "0") == "0"
    assert "not logged in" in response.get("AF", "").lower()
    assert [name for name, _ in fake_ils.calls] == []


def test_99_is_allowed_before_login(dispatcher):
    _, response = send(dispatcher, Session(), "9900402.00")
    assert response.code == "98" and response.fixed["online_status"] == "Y"


def test_login_then_circulation(dispatcher, fake_ils):
    session = Session()
    login(dispatcher, session)
    _, response = send(dispatcher, session, CHECKOUT, 1)
    assert response.code == "12" and response.ok
    assert fake_ils.calls_to("checkout")[0]["caller"].user_pid == "42"


def test_unsupported_message_gets_a_well_formed_refusal(dispatcher, session):
    outcome, response = send(dispatcher, session, f"15+{NOW}AOCERN|AAP001|ABIT001|")
    assert not outcome.close
    assert response.code == "16"
    assert response.checksum_ok is True


def test_repeated_failed_logins_close_the_connection(dispatcher, settings):
    session = Session()
    for attempt in range(1, settings.max_login_attempts + 1):
        outcome, response = send(dispatcher, session, "9300CNkiosk1|COwrong|CP|", attempt)
        assert not response.ok
    assert outcome.close


def test_handler_crash_becomes_unavailable_refusal(dispatcher, fake_ils, session, caplog):
    fake_ils.answers["get_item"] = RuntimeError("bug")
    with caplog.at_level(logging.ERROR):
        _, response = send(dispatcher, session, f"17{NOW}AOCERN|ABIT001|")
    assert response.code == "18"
    assert "unavailable" in response.get("AF").lower()
    assert "handler crashed" in caplog.text


def test_crash_during_a_write_is_ambiguous_and_never_replayed(dispatcher, fake_ils, session):
    fake_ils.answers["checkout"] = RuntimeError("bug after the ILS acted?")
    _, first = send(dispatcher, session, CHECKOUT, 4)
    assert "check at the library desk" in first.get("AF").lower()

    fake_ils.answers["checkout"] = CHECKOUT_RESULT
    _, second = send(dispatcher, session, CHECKOUT, 4)
    assert second.ok and len(fake_ils.calls_to("checkout")) == 2


# --------------------------------------------------------------- deadline


@pytest.mark.parametrize(
    "body, name, message",
    [
        (CHECKOUT, "checkout", "could not confirm"),
        (f"17{NOW}AOCERN|ABIT001|", "get_item", "unavailable"),
    ],
)
def test_deadline_answers_before_the_kiosk_gives_up(
    handlers, settings, fake_ils, session, caplog, body, name, message
):
    from cds_sip2.sip2.dispatcher import Dispatcher

    async def hang(**kwargs):
        await asyncio.sleep(60)

    fake_ils.answers[name] = hang
    dispatcher = Dispatcher(handlers, replace(settings, request_deadline=0.05))

    with caplog.at_level(logging.ERROR):
        _, response = send(dispatcher, session, body, 2)

    assert message in response.get("AF").lower()
    assert not session.replayable  # a resend must ask the ILS again
    assert "deadline exceeded" in caplog.text


# --------------------------------------------------------------- resends


def test_identical_resend_is_answered_from_cache(dispatcher, fake_ils, session):
    first, _ = send(dispatcher, session, CHECKOUT, 4)
    again, _ = send(dispatcher, session, CHECKOUT, 4)

    assert again.reply == first.reply
    assert len(fake_ils.calls_to("checkout")) == 1


def test_resend_after_ambiguous_asks_the_ils_again_with_same_id(dispatcher, fake_ils, session):
    fake_ils.answers["checkout"] = ILSAmbiguous("timeout", "x")
    _, first = send(dispatcher, session, CHECKOUT, 4)
    assert not first.ok
    assert "check at the library desk" in first.get("AF").lower()

    fake_ils.answers["checkout"] = CHECKOUT_RESULT
    _, second = send(dispatcher, session, CHECKOUT, 4)

    assert second.ok
    ids = [call["caller"].transaction_id for call in fake_ils.calls_to("checkout")]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_same_frame_after_another_request_is_not_a_resend(dispatcher, fake_ils, session):
    send(dispatcher, session, CHECKOUT, 1)
    send(dispatcher, session, "9900402.00", 2)
    send(dispatcher, session, CHECKOUT, 1)
    assert len(fake_ils.calls_to("checkout")) == 2


# --------------------------------------------------------- transaction ids


def txid(body, sequence=0, terminal="kiosk1"):
    return transaction_id(replace(request(body), sequence=sequence), terminal)


def test_transaction_id_is_stable_across_connections(dispatcher, fake_ils):
    for _ in range(2):
        session = Session()
        login(dispatcher, session)
        send(dispatcher, session, CHECKOUT, 5)
    ids = {call["caller"].transaction_id for call in fake_ils.calls_to("checkout")}
    assert len(ids) == 1
    assert len(ids.pop()) == 32


def test_transaction_id_separates_what_should_be_separate():
    base = txid(CHECKOUT)
    assert txid(CHECKOUT) == base
    assert txid(CHECKOUT, terminal="kiosk2") != base
    assert txid(CHECKOUT, sequence=1) != base
    assert txid(CHECKOUT.replace("ABIT001", "ABIT002")) != base
    assert txid(CHECKOUT.replace("AAP001", "AAP002")) != base
    assert txid(CHECKOUT.replace(NOW, "20261102    120001", 1)) != base
    # checkout then checkin of the same item never collide
    assert txid(f"09N{NOW}{NOW}AP|AOCERN|ABIT001|AC|") != base


def test_transaction_id_ignores_pin_and_terminal_password():
    assert txid(CHECKOUT) == txid(CHECKOUT.replace("AD1234", "AD9999"))
    assert txid(CHECKOUT) == txid(CHECKOUT.replace("AC|", "ACpass|"))


# --------------------------------------------------------------- log safety


def test_pin_and_passwords_never_reach_any_log_level(dispatcher, fake_ils):
    """Drive every path that touches AD/CO/AC with the root logger at DEBUG.

    The secrets are not hex, so they cannot turn up by chance in a hash.
    """
    stream = io.StringIO()
    log.configure("DEBUG", "key", stream=stream)
    try:
        session = Session()
        send(dispatcher, session, "9300CNkiosk1|COsup3rs3cret|CP|", 0)
        send(dispatcher, session, f"63001{NOW}{' ' * 10}AOCERN|AAP001|ACt3rmpw|ADpq86z|", 1)
        send(dispatcher, session, f"23001{NOW}AOCERN|AAP001|ACt3rmpw|ADpq86z|", 2)
        send(dispatcher, session, CHECKOUT.replace("AD1234", "ADpq86z"), 3)
        fake_ils.answers["checkout"] = ILSAmbiguous("timeout", "x")
        send(dispatcher, session, CHECKOUT.replace("AD1234", "ADpq86z"), 4)
        run(dispatcher.dispatch(frame("9300CNkiosk1|COsup3rs3cret|CP|")[:-4] + b"0000", session))
        run(dispatcher.dispatch(b"\xffCOsup3rs3cret|ADpq86z|", session))
        send(dispatcher, Session(), f"11YN{NOW}{BLANK}AOCERN|AAP001|ABIT1|ACt3rmpw|ADpq86z|")
    finally:
        logging.getLogger().handlers.clear()

    output = stream.getvalue()
    assert output.count("\n") > 10
    for secret in ("sup3rs3cret", "pq86z", "t3rmpw", "P001", "Ada Lovelace"):
        assert secret not in output, secret
