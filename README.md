# cds-sip2

A SIP2 gateway for CDS-ILS.

Self-check kiosks and RFID hardware in libraries communicate using a protocol called SIP2, whereas
CDS-ILS exposes an HTTP interface. This project bridges the two. It is a small asyncio SIP2
server that translates each incoming SIP2 message into a REST call against a dedicated
`/api/sip2` API on the ILS.

The service intentionally contains no circulation logic. It does not decide who may borrow an
item, for how long, or whether a patron is blocked. Those decisions belong to the ILS, and the
service's only responsibility is to relay the ILS's answer in SIP2 form.

## Status

> **Draft, published for discussion of the approach and structure.**
>
> - The `/api/sip2` endpoints on CDS-ILS do not yet exist. The contract described below is a
>   proposal, and `tests/fake_ils.py` is currently its only implementation.
> - The service has not been deployed to any environment.
> - Earlier versions ran on invenio-sip2. That dependency (and with it Flask, Redis and
>   SQLAlchemy) has been removed; see [Design decisions](#design-decisions).

## Architecture

```
 kiosk / RFID tablet (stunnel client)
        │  SIP2 over TLS
        ▼
 ┌──────────────── OpenShift pod ────────────────┐
 │ stunnel sidecar (TLS termination)             │
 │        │  plaintext SIP2 on 127.0.0.1:3004    │
 │        ▼                                      │
 │ cds-sip2 (this repository)    :8080 /livez    │
 │                                     /readyz   │
 └────────┼──────────────────────────────────────┘
          │  HTTPS with bearer token
          ▼
 CDS-ILS  /api/sip2/*
```

SIP2 is a line-based protocol in which the device sends a two-digit message code followed by a
series of fields (for example `93` for login, `63` for a patron information request, and `11` for
a checkout). The server replies with the corresponding response code. The 3M SIP2 specification
is the authoritative reference.

The application is two packages with a strict boundary. `cds_sip2.sip2` speaks SIP2 and knows
nothing about HTTP; `cds_sip2.ils` speaks HTTP and knows nothing about SIP2. They meet only in
`cds_sip2/__main__.py`, and the SIP2 side depends only on the `ILSBackend` Protocol in
`cds_sip2/ils/interface.py`.

## Repository layout

| Path | Purpose |
|---|---|
| `cds_sip2/__main__.py` | Composition root: reads settings, builds the ILS client, starts the SIP2 and health listeners, handles SIGTERM. |
| `cds_sip2/config.py` | Frozen `Settings`, read once from `CDS_SIP2_*` environment variables. |
| `cds_sip2/logging.py` | JSON logs to stdout, and the keyed hash used for patron barcodes. |
| `cds_sip2/health.py` | `/livez` and `/readyz` for OpenShift probes. |
| `cds_sip2/ils/interface.py` | The `ILSBackend` Protocol: the seam between the two packages. |
| `cds_sip2/ils/client.py` | The async HTTP client for the ILS (httpx). Its docstrings also serve as the written contract. |
| `cds_sip2/ils/errors.py` | `ILSError` and its subclasses, including `ILSAmbiguous`. |
| `cds_sip2/sip2/framing.py`, `checksum.py` | CR-terminated frames with a size limit; the AY/AZ trailer. |
| `cds_sip2/sip2/messages.py`, `enums.py` | Declarative field tables for every SIP2 2.00 message, and the code tables. |
| `cds_sip2/sip2/codec.py` | Frame ↔ `Request`/`Response`, sanitising of outgoing fields, redaction for logs. |
| `cds_sip2/sip2/session.py` | Per-connection state (terminal, acting user, last exchange). |
| `cds_sip2/sip2/dispatcher.py` | Routing, login gating, 96/97, resends, transaction ids. |
| `cds_sip2/sip2/handlers.py` | One method per supported message: request → `ILSBackend` → response. |
| `cds_sip2/sip2/server.py` | asyncio TCP server, connection limits, graceful shutdown. |
| `cds_sip2/utils.py` | Barcode normalisation and SIP2 date formatting. |
| `tests/sip2_client.py` | A scripted SIP2 client that depends only on the standard library. It can also be used as a command-line tool against a deployed server. |
| `tests/fake_ils.py` | An in-memory implementation of the ILS API, used for tests and local development. |
| `Dockerfile`, `entrypoint.sh` | The container image and its entrypoint (`server`, `healthcheck`, or an arbitrary command). |
| `docker-compose.yml` | A local stack comprising the fake ILS and the service, without TLS. |
| `pyproject.toml`, `requirements.in`, `requirements.txt` | Packaging. The only runtime dependency is `httpx`. |

## Running the service locally

Docker is required. Python is needed only to run the tests or the client outside a container.

```bash
docker compose up --build -d
```

This starts a fake ILS on port 5001 and the service on port 3004 (SIP2) and 8080 (health). The
ports are published on all interfaces for convenience and the fake credentials are publicly
known, so the stack should be run on a personal workstation rather than a shared host.

Without Docker, the same works with `python -m tests.fake_ils --port 5001` in one terminal and
`CDS_SIP2_ILS_URL=http://127.0.0.1:5001/api/sip2 CDS_SIP2_ILS_TOKEN=test-token python -m cds_sip2`
in another.

A scripted session can then be run against it:

```bash
python -m tests.sip2_client --port 3004 \
    --terminal kiosk1 --password secret \
    --patron P001 --pin 1234 \
    --item CM-B000456412
```

The script logs in, requests the server status, looks up the patron and the item, checks the item
out and back in, and ends the session. Each frame is printed along with a pass or fail result for
every step.

The fake ILS contains the following data and keeps it in memory, so restarting it resets the state.

| Entity | Values |
|---|---|
| Terminal | `kiosk1` with password `secret` |
| Patrons | `P001` (PIN `1234`, in good standing) and `P002` (PIN `0000`, blocked from borrowing) |
| Items | `CM-B000456412` (available), `CM-1234567890` and `CM-P00101093` (both on loan to `P001`) |

To follow the service logs, use `docker compose logs -f sip2`. Each line is one JSON object. To
stop the stack, use `docker compose down`.

## Running the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -e ".[tests]"
./run-tests.sh
```

No external services are needed.

- `unit/test_ils_client.py` covers the HTTP contract, timeouts, error mapping including the
  ambiguous cases, and the absence of retries (httpx `MockTransport`).
- `unit/test_handlers.py` exercises each handler against an in-memory `ILSBackend`.
- `unit/test_codec.py` covers checksums, parsing, sanitising and log redaction.
- `unit/test_dispatcher.py` covers 96/97, login gating, resends, transaction ids, and checks that
  PINs and passwords never reach the log at any level.
- `unit/test_server.py` covers the TCP server (limits, timeouts, graceful shutdown) and the health
  endpoints.
- `unit/test_sip2_client.py` verifies the framing, checksum and parsing logic of the test client.
- `integration/test_sip2_protocol.py` starts `python -m cds_sip2` as a subprocess against the fake
  ILS and drives it over TCP: login, the 98 capabilities, a full session including a refused
  checkout, unknown patrons and items, a blocked patron, unsupported messages, 96 and 97, an
  idempotent resend across a reconnect, health endpoints, and a clean exit on SIGTERM.

## Protocol behaviour

| Request | Response | ILS call |
|---|---|---|
| 93 Login | 94 | `POST /login` |
| 99 SC Status | 98 ACS Status | `GET /ping`. Capability flags and `BX` are derived from the handler routes. |
| 23 Patron Status | 24 | `GET /patrons/{barcode}`, plus `POST /patrons/{barcode}/verify` when the device sends AD |
| 25 Patron Enable | 26 | As 23. Reports the current status; it cannot lift a block. |
| 63 Patron Information | 64 | As 23. The list asked for in `summary` is sent as repeated fields, sliced by BP/BQ. |
| 17 Item Information | 18 | `GET /items/{barcode}` |
| 11 Checkout | 12 | `POST /checkout` |
| 09 Checkin | 10 | `POST /checkin` |
| 29 Renew | 30 | `POST /renew` |
| 35 End Patron Session | 36 | None. No patron state is kept. |
| 97 Request ACS Resend | Last response, byte for byte | None |

Block Patron (01), Hold (15), Item Status Update (19), Fee Paid (37) and Renew All (65) are
advertised as unsupported in `BX` and answered with a well-formed refusal (for example a `16`
with ok=0) carrying a screen message, rather than a dropped connection.

Framing and error detection:

- Frames end in CR; a LF after the CR is tolerated. A frame longer than
  `CDS_SIP2_MAX_FRAME_BYTES`, input that is not text in the configured encoding, or a line that
  does not start with a two-digit code closes the connection with a log entry.
- With `CDS_SIP2_ERROR_DETECTION` on, a request with a bad checksum, or a known message whose
  fixed part is truncated, receives a `96`. Requests without a trailer are accepted. Every
  response echoes the request's sequence number and carries a valid checksum. Like `97`, `96`
  itself carries no trailer, because the sequence number of a corrupted frame cannot be trusted.
  A `96` is never stored as the last response, so a following `97` replays the last real one.
- An unknown message code receives a `96`.
- Before a successful `93`, only `93` and `99` are processed. Anything else receives the refusal
  for that message with "Terminal not logged in". After `CDS_SIP2_MAX_LOGIN_ATTEMPTS` failed
  logins the connection is closed.

Sessions live exactly as long as the TCP connection: the terminal, the acting `user_pid` and the
last exchange are held in memory and lost on disconnect, so a kiosk must log in again after
reconnecting. Nothing is shared between connections or replicas.

## ILS API contract

All requests carry the header `Authorization: Bearer <CDS_SIP2_ILS_TOKEN>`. The ILS maintains the
terminal registry, which associates each terminal with its password, acting user and location, so
the service never needs to know about locations. Each circulation request passes `terminal` and
`user_pid` (both from the session established at login) for this purpose.

| Method and path | Request body | Successful (200) response |
|---|---|---|
| `GET /ping` | None | `{"ok": true}` |
| `POST /login` | `{"terminal", "password", "ip"}` | `{"ok": true, "user_pid", "location_pid", "institution_id", "library_name", "language"}`, or `{"ok": false}` for invalid credentials |
| `GET /patrons/{barcode}` | None | `{"barcode", "name", "email", "status_flags": [...], "loans": [], "overdue": [], "holds": [], "recalls": [], "unavailable_holds": [], "fines": [], "fines_total", "currency"}` |
| `POST /patrons/{barcode}/verify` | `{"pin"}` | `{"ok": true\|false}` |
| `GET /items/{barcode}` | None | `{"barcode", "title", "status", "location", "current_location", "due_date", "hold_queue_length", "owner"}` |
| `POST /checkout` | `{"item_barcode", "patron_barcode", "user_pid", "terminal", "transaction_id"}` | `{"title", "due_date", "is_renewal"}` |
| `POST /checkin` | `{"item_barcode", "user_pid", "terminal", "transaction_id"}` | `{"title", "permanent_location", "alert"}` |
| `POST /renew` | `{"item_barcode", "patron_barcode", "user_pid", "terminal", "transaction_id"}` | `{"title", "due_date"}` |

The `ip` sent on login is the address the server sees, which behind stunnel is always loopback.

The service interprets responses as follows.

| Situation | Exception | Kiosk sees | Log |
|---|---|---|---|
| `404` on a patron or item lookup | none (`None`) | Invalid patron or unknown item | INFO |
| `404` on checkout, checkin or renew (unknown item) | `ILSNotFound` | Refusal with the `unknown_item` message | INFO |
| `409` or another `4xx`, or `{"ok": false}` on a circulation call | `ILSRejected` | Refusal with the ILS's `user_message` | INFO |
| Connect failure; read timeout or `5xx` on a lookup | `ILSUnavailable` | Generic "unavailable" | ERROR |
| Circulation request sent but no answer: read timeout, reset, `5xx` | `ILSAmbiguous` | "Could not confirm the transaction. Please check at the library desk." | ERROR, with `transaction_id` |
| `401` or `403` | `ILSConfigError` | Generic "unavailable" | ERROR, `alert: true` |

- **Unknown items on circulation calls use `404`** with a body such as
  `{"code": "item_not_found"}`. An unknown *patron* on a circulation call is a refusal (`409`).
- **Nothing is ever retried by the HTTP client**, because retrying a checkout could record the
  loan twice.
- **Patron status flags** are the 14 SIP2 patron-status names, for example
  `charge_privileges_denied` or `excessive_outstanding_fines` (the same values invenio-sip2's
  `PatronStatusTypes` used). Unknown flags are logged and ignored.
- **Item status** is one of `available`, `on_loan`, `recalled`, `on_hold_shelf`, `in_transit`,
  `in_process`, `on_order`, `lost`, `missing` or `other`. Anything else is reported as "other"
  with a warning.
- **Dates** use ISO-8601. A date without a time is presented to the kiosk as the end of that day.

### Idempotency

`transaction_id` is derived from the content of the SIP2 request: a hash of the terminal, the
message code, the sequence number, the fixed fields (including the kiosk's transaction date) and
the institution, patron and item barcodes. PINs and passwords are excluded. A kiosk resending the
same frame after a timeout or a `96`, even over a new connection, therefore produces the same
id, while a later checkin of the same item, or a new request with a new transaction date or
sequence number, produces a different one.

**The ILS must answer a duplicate `transaction_id` with the original result, not with an error**,
for a window still to be agreed (see Open questions). The fake ILS does this, without expiry.

Separately, each connection remembers its last request and response. A `97` replays the response,
and an identical resend is answered from that cache without a second ILS call, unless the cached
answer was an ambiguous refusal, in which case the ILS is asked again with the same id.

## Logging

Logs are JSON, one object per line, on stdout only. Every request and response frame is logged at
INFO as a redacted rendering (the "simple logging interface"):

- `AD` (patron PIN), `CO` (login password) and `AC` (terminal password) are masked inside the
  codec, so no log level can reveal them.
- `AA` (patron barcode) is replaced by a truncated keyed hash (`p:` + 12 hex digits). Support can
  correlate entries for one patron without the log revealing who it is. The key comes from
  `CDS_SIP2_LOG_HASH_KEY`; without it, a random key is used and pseudonyms change on restart.
- `AE`, `BD`, `BE`, `BF` (name, address, email, phone) are dropped.
- Frames that cannot be parsed are logged only by code and length.

## Health and shutdown

- `GET /livez` returns 200 while the SIP2 listener is accepting. Use it for the liveness probe.
- `GET /readyz` additionally requires the ILS to answer its ping, cached for
  `CDS_SIP2_HEALTH_ILS_CACHE` seconds. Use it for the readiness probe: an ILS outage makes the pod
  unready but never restarts it.
- On SIGTERM the server stops accepting, closes idle connections, lets requests in flight finish
  for up to `CDS_SIP2_SHUTDOWN_GRACE` seconds (to avoid ambiguous outcomes during deploys), then
  exits. Set the pod's `terminationGracePeriodSeconds` above that value.

## Design decisions

The following choices are those on which feedback is most welcome.

- **A separate service rather than a component of the ILS.** SIP2 is a long-lived socket protocol
  with its own operational concerns, such as TLS termination, per-device sessions and
  hardware-specific behaviour. Keeping it outside the ILS allows it to be deployed, scaled and
  diagnosed independently.
- **No circulation logic in the service.** Loan periods, blocks, renewals and holds are all
  determined by the ILS. The handlers only translate between formats, which is also why the ILS
  owns the terminal registry.
- **Our own SIP2 server instead of invenio-sip2.** invenio-sip2 requires a full Invenio
  application (Flask, SQLAlchemy, Celery, Redis for sessions) to run a socket server. The SIP2
  side here is a few hundred lines of declarative message tables plus an asyncio server, with
  one runtime dependency (httpx), and every protocol rule above is under our control and tested.
- **Async HTTP.** The server is asyncio, so the ILS client is too. A slow ILS call never blocks
  other kiosks.
- **Distinct treatment of refusals, outages and ambiguous outcomes.** When the ILS declines a
  transaction, the kiosk receives a clean refusal containing the ILS's own message. When the ILS
  is unreachable, the kiosk receives a generic "unavailable" message. When a circulation request
  may have been recorded, the kiosk is told to check at the desk and the log carries the
  transaction id. In every case the device receives a well-formed SIP2 reply.
- **Session state in memory.** Sessions are per connection, so Redis is unnecessary; idempotency
  across reconnects comes from content-derived transaction ids instead.

## Open questions

1. **Resend behaviour of the real hardware.** The transaction id assumes a kiosk resends with the
   same sequence number and transaction date. If the hardware regenerates either, resends will
   not be deduplicated. This needs testing with the actual devices.
2. **Duplicate window.** How long the ILS honours a duplicate `transaction_id` must be agreed and
   written into the contract above.
3. **Timeouts.** The ILS timeouts (3 seconds to connect, 8 to read), `CDS_SIP2_TIMEOUT_PERIOD`
   and `CDS_SIP2_RETRIES_ALLOWED` should be tuned against the hardware. The 98 timeout field is
   sent verbatim; the spec's unit for it (seconds or tenths) should be confirmed against the 3M
   document and the device's behaviour.
4. **Readiness during an ILS outage.** With one replica, an unready pod means kiosks cannot
   connect at all rather than seeing "unavailable" in a 98. Decide whether readiness should
   track the ILS.

## Known limitations

- **SIP2 is unencrypted by nature.** TLS is terminated by the stunnel sidecar. Without it,
  passwords and PINs are transmitted in clear text.
- **Per-IP limits must be applied at the edge.** Behind stunnel the server sees every connection
  as coming from loopback; it enforces only a global connection limit and per-connection login
  attempts.
- **Due-date times use the container's timezone**, which is typically UTC.
- **Screen messages are available in English only**, although the language is configurable.
- **All items are reported as books.** Security markers are not used, since the hardware relies
  on RFID.

## Configuration

All settings are environment variables prefixed with `CDS_SIP2_`. An invalid value stops the
service at start-up with exit code 2.

| Variable | Default | Notes |
|---|---|---|
| `CDS_SIP2_ILS_URL` | `http://localhost:5000/api/sip2` | Base URL of the ILS API |
| `CDS_SIP2_ILS_TOKEN` | empty | Bearer token for the ILS. Must be supplied from a secret. |
| `CDS_SIP2_ILS_CONNECT_TIMEOUT`, `CDS_SIP2_ILS_READ_TIMEOUT` | `3`, `8` (seconds) | Keep their sum well below the kiosk's own timeout |
| `CDS_SIP2_BIND_HOST`, `CDS_SIP2_PORT` | `127.0.0.1`, `3004` | SIP2 listener; loopback behind stunnel |
| `CDS_SIP2_MAX_CONNECTIONS` | `50` | Further connections are closed immediately |
| `CDS_SIP2_IDLE_TIMEOUT` | `300` (seconds) | Time without a complete frame before the connection is closed |
| `CDS_SIP2_MAX_FRAME_BYTES` | `4096` | Longer input closes the connection |
| `CDS_SIP2_SHUTDOWN_GRACE` | `10` (seconds) | Time in-flight requests may take after SIGTERM |
| `CDS_SIP2_ERROR_DETECTION` | `true` | Verify AZ on requests, emit AY/AZ on responses |
| `CDS_SIP2_ENCODING` | `utf-8` | Text encoding on the wire |
| `CDS_SIP2_TIMEOUT_PERIOD`, `CDS_SIP2_RETRIES_ALLOWED` | `15`, `3` | Advertised to the kiosk in the 98 response |
| `CDS_SIP2_MAX_LOGIN_ATTEMPTS` | `3` | Failed 93s before the connection is closed |
| `CDS_SIP2_INSTITUTION_ID`, `CDS_SIP2_LIBRARY_NAME`, `CDS_SIP2_LANGUAGE` | `CERN`, `CERN Library`, `en` | Fallbacks used when the ILS login response omits them |
| `CDS_SIP2_HEALTH_HOST`, `CDS_SIP2_HEALTH_PORT` | `0.0.0.0`, `8080` | Health listener; must be reachable by the kubelet |
| `CDS_SIP2_HEALTH_ILS_CACHE` | `10` (seconds) | How long `/readyz` reuses an ILS ping |
| `CDS_SIP2_LOG_LEVEL` | `INFO` | Secrets are masked at every level, including `DEBUG` |
| `CDS_SIP2_LOG_HASH_KEY` | random per process | Key for patron pseudonyms. Set from a secret. |

The invenio-era variables (`SIP2_DATASTORE_REDIS_URL`, `SIP2_TIMEOUT_PERIOD`, `SIP2_PORT`,
`SIP2_BIND_HOST`, `SECRET_KEY`, `INVENIO_*`, ...) are no longer read.
