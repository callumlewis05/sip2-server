# cds-sip2

A SIP2 gateway for CDS-ILS.

Self-check kiosks and RFID hardware in libraries communicate using a protocol called SIP2, whereas
CDS-ILS exposes an HTTP interface. This project bridges the two. It is a small asyncio SIP2
server that translates each incoming SIP2 message into a REST call against a dedicated
`/api/sip2` API on the ILS.

The current scope is the inventory side: item lookups, checkout, checkin and item status
updates. Patron messages, renewals and the rest are refused as unsupported for now.

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
nothing about HTTP; `cds_sip2.rest` speaks HTTP and knows nothing about SIP2. Both depend on
`cds_sip2/domain.py`, which holds the typed values passed between them and the `ILSBackend`
Protocol the SIP2 side calls. They meet only in `cds_sip2/__main__.py`. See
[ARCHITECTURE.md](ARCHITECTURE.md) for a tour.

## Repository layout

| Path | Purpose |
|---|---|
| `cds_sip2/__main__.py` | Composition root: reads settings, builds the ILS client, starts the SIP2 and health listeners, handles SIGTERM. |
| `cds_sip2/config.py` | `CDS_SIP2_*` defaults as module variables, overridable by environment variables of the same name; frozen `Settings`. |
| `cds_sip2/domain.py` | Domain types (`Item`, `CheckoutResult`, `CheckinResult`, `ItemStatus`, `MediaType`, `Caller`, ...) and the `ILSBackend` Protocol. |
| `cds_sip2/errors.py` | `ILSError` and its subclasses, including `ILSAmbiguous`. |
| `cds_sip2/logging.py` | JSON logs to stdout, and the keyed hash used for patron barcodes. |
| `cds_sip2/health.py` | `/livez` and `/readyz` for OpenShift probes. |
| `cds_sip2/rest/client.py` | The async HTTP client for the ILS (httpx). Parses every payload into domain types. Its docstrings also serve as the written contract. |
| `cds_sip2/sip2/framing.py`, `checksum.py` | CR-terminated frames with a size limit; the AY/AZ trailer. |
| `cds_sip2/sip2/messages.py`, `enums.py` | Declarative field tables for every SIP2 2.00 message; the code tables and the maps from domain enums onto them. |
| `cds_sip2/sip2/mapper.py` | Typed request and response classes per message, checked against `messages.py` at import; the generic refusal. |
| `cds_sip2/sip2/codec.py` | Frame ↔ `Request`/`Response`, sanitising of outgoing fields, redaction for logs. |
| `cds_sip2/sip2/session.py` | Per-connection state (terminal, acting user, last exchange). |
| `cds_sip2/sip2/dispatcher.py` | Routing, login gating, 96/97, resends, transaction ids, the per-request deadline. |
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
`CDS_SIP2_ILS_URL=http://127.0.0.1:5001/api/sip2 CDS_SIP2_ILS_TOKEN=test-token
CDS_SIP2_TERMINALS='{"kiosk1": {"password": "secret", "user_pid": "42"}}' python -m cds_sip2`
in another.

A scripted session can then be run against it:

```bash
python -m tests.sip2_client --port 3004 \
    --terminal kiosk1 --password secret \
    --patron P001 --item CM-B000456412 --status-update shelf-1
```

The script logs in, requests the server status, looks up the item, sends an item status update,
and checks the item out and back in. Each frame is printed along with a pass or fail result for
every step.

The terminal `kiosk1` / `secret` is configured on the SIP2 server itself (`CDS_SIP2_TERMINALS`).
The fake ILS contains the following data and keeps it in memory, so restarting it resets the state.

| Entity | Values |
|---|---|
| Patrons | `P001` (in good standing) and `P002` (blocked from borrowing); used only to accept or refuse checkouts |
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

- `unit/test_rest_client.py` covers the HTTP contract, payload parsing into domain types, error
  mapping including the ambiguous cases, and the absence of retries (httpx `MockTransport`).
- `unit/test_handlers.py` exercises each handler against an in-memory `ILSBackend`.
- `unit/test_codec.py` covers checksums (including strict mode), parsing, sanitising and log
  redaction.
- `unit/test_dispatcher.py` covers 96/97, login gating, resends, transaction ids, the request
  deadline, and checks that PINs and passwords never reach the log at any level.
- `unit/test_config.py` covers the module defaults, environment overrides and validation.
- `unit/test_server.py` covers the TCP server (limits, timeouts, graceful shutdown) and the health
  endpoints.
- `unit/test_sip2_client.py` verifies the framing, checksum and parsing logic of the test client.
- `integration/test_sip2_protocol.py` starts `python -m cds_sip2` as a subprocess against the fake
  ILS and drives it over TCP: login, the 98 capabilities, a full inventory session including a
  status update and a refused checkout, unknown items, a blocked patron, unsupported messages
  (patron, renew), 96 and 97, an
  idempotent resend across a reconnect, health endpoints, and a clean exit on SIGTERM.

## Protocol behaviour

| Request | Response | ILS call |
|---|---|---|
| 93 Login | 94 | None. `CN`/`CO` are checked against `CDS_SIP2_TERMINALS`. |
| 99 SC Status | 98 ACS Status | `GET /ping`. Capability flags and `BX` are derived from the handler routes. |
| 17 Item Information | 18 | `GET /items/{barcode}` |
| 19 Item Status Update | 20 | `POST /items/{barcode}/status`, passing `CH` verbatim |
| 11 Checkout | 12 | `POST /checkout` |
| 09 Checkin | 10 | `POST /checkin` |
| 97 Request ACS Resend | Last response, byte for byte | None |

Patron Status (23), Patron Enable (25), Patron Information (63), Renew (29), End Patron Session
(35), Block Patron (01), Hold (15), Fee Paid (37) and Renew All (65) are advertised as
unsupported in `BX` and answered with a well-formed refusal (for example a `16` with ok=0)
carrying a screen message, rather than a dropped connection. Each refusal comes from the
message's response class in `sip2/mapper.py`, whose defaults are the refused state.

The media type (`CK`) comes from the item's `media_type` in the ILS, or
`CDS_SIP2_DEFAULT_MEDIA_TYPE` when the record has none. The security marker is
`CDS_SIP2_SECURITY_MARKER` for every item.

Framing and error detection:

- Frames end in CR; a LF after the CR is tolerated. A frame longer than
  `CDS_SIP2_MAX_FRAME_BYTES`, input that is not text in the configured encoding, or a line that
  does not start with a two-digit code closes the connection with a log entry.
- With `CDS_SIP2_ERROR_DETECTION` on, a request with a bad checksum, or a known message whose
  fixed part is truncated, receives a `96`. Requests without a trailer are accepted unless
  `CDS_SIP2_REQUIRE_CHECKSUM` is on (`97` is always exempt). Every
  response echoes the request's sequence number and carries a valid checksum. Like `97`, `96`
  itself carries no trailer, because the sequence number of a corrupted frame cannot be trusted.
  A `96` is never stored as the last response, so a following `97` replays the last real one.
- An unknown message code receives a `96`.
- Before a successful `93`, only `93` and `99` are processed. Anything else receives the refusal
  for that message with "Terminal not logged in". After `CDS_SIP2_MAX_LOGIN_ATTEMPTS` failed
  logins the connection is closed.
- Each request is handled within `CDS_SIP2_REQUEST_DEADLINE` seconds, ILS calls included. On
  expiry the kiosk gets a refusal ("could not confirm" for a write), which is never replayed from
  the cache. The deadline must be below the advertised `CDS_SIP2_TIMEOUT_PERIOD`, so the server
  answers before the kiosk gives up and resends.

Sessions live exactly as long as the TCP connection: the terminal, the acting `user_pid` and the
last exchange are held in memory and lost on disconnect, so a kiosk must log in again after
reconnecting. Nothing is shared between connections or replicas.

## ILS API contract

All requests carry the header `Authorization: Bearer <CDS_SIP2_ILS_TOKEN>`, which identifies this
service; there is no per-terminal login against the ILS. Terminal credentials and the `user_pid`
each terminal acts as are configured on this service (`CDS_SIP2_TERMINALS`, from a Secret). Each
write passes `terminal`, `user_pid` and `transaction_id` (the domain `Caller`), and the ILS
resolves the transaction location from `terminal`.

| Method and path | Request body | Successful (200) response |
|---|---|---|
| `GET /ping` | None | `{"ok": true}` |
| `GET /items/{barcode}` | None | `{"barcode", "title", "status", "location", "current_location", "due_date", "hold_queue_length", "owner", "media_type", "messages"}` (only `status` required) |
| `POST /items/{barcode}/status` | `{"item_barcode", "properties", "user_pid", "terminal", "transaction_id"}` | The item, as for `GET /items/{barcode}` |
| `POST /checkout` | `{"item_barcode", "patron_barcode", "user_pid", "terminal", "transaction_id"}` | `{"title", "due_date", "is_renewal", "media_type", "messages"}` |
| `POST /checkin` | `{"item_barcode", "user_pid", "terminal", "transaction_id"}` | `{"title", "permanent_location", "alert", "media_type", "messages"}` |

`properties` is the SIP2 `CH` value of a 19 Item Status Update, unchanged. What it means (for an
inventory wand, typically "seen at <location>") is to be agreed with the ILS; see Open questions.

The service interprets responses as follows.

| Situation | Exception | Kiosk sees | Log |
|---|---|---|---|
| `404` on an item lookup | none (`None`) | Unknown item | INFO |
| `404` on a write (unknown item) | `ILSNotFound` | Refusal with the `unknown_item` message | INFO |
| `409` or another `4xx`, or `{"ok": false}` on a write | `ILSRejected` | Refusal with the ILS's `user_message` | INFO |
| Connect failure; read timeout, `5xx` or an unreadable body on a lookup | `ILSUnavailable` / `ILSError` | Generic "unavailable" | ERROR |
| Write sent but no usable answer: read timeout, reset, `5xx`, empty or unreadable `2xx` body | `ILSAmbiguous` | "Could not confirm the transaction. Please check at the library desk." | ERROR, with `transaction_id` |
| `401` or `403` | `ILSConfigError` | Generic "unavailable" | ERROR, `alert: true` |

- **Unknown items on writes use `404`** with a body such as `{"code": "item_not_found"}`. An
  unknown *patron* on a checkout is a refusal (`409`).
- **Nothing is ever retried by the HTTP client**, because retrying a checkout could record the
  loan twice.
- **Item status** is one of the `ItemStatus` values in `cds_sip2/domain.py`: `available`,
  `on_loan`, `recalled`, `on_hold_shelf`, `in_transit`, `in_process`, `on_order`, `lost`,
  `missing` or `other`. Anything else is parsed as `other`, with a warning from the client.
- **Media type** is one of the `MediaType` values in `cds_sip2/domain.py` (`book`, `magazine`,
  `video`, `cd`, ...). Anything else, or none, falls back to `CDS_SIP2_DEFAULT_MEDIA_TYPE`.
- **Patron blocks**, for when patron messages are supported, are the `PatronBlock` values in
  `cds_sip2/domain.py` (`charge_privileges_denied`, ...). Their SIP2 field positions are listed
  explicitly in `sip2/enums.py`, so the REST vocabulary does not depend on the SIP2 flag order.
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
INFO as a redacted rendering (the "simple logging interface"), built from an allow-list:

- Tags known to carry no personal data (`LOGGABLE_TAGS` in `sip2/codec.py`: institution, item
  barcode, title, dates, locations, terminal id, ...) are shown as sent.
- `AA` (patron barcode) is replaced by a truncated keyed hash (`p:` + 12 hex digits). Support can
  correlate entries for one patron without the log revealing who it is. The key comes from
  `CDS_SIP2_LOG_HASH_KEY`; without it, a random key is used and pseudonyms change on restart.
- Every other tag is masked as `XX~`: PINs and passwords (`AD`, `CO`, `AC`), names, addresses,
  screen messages, item properties, and any tag added in future until it is put on the list.
  This happens inside the codec, so no log level can reveal them.
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
  determined by the ILS. The handlers only translate between formats.
- **Three layers between kiosk and ILS.** `sip2/mapper.py` turns tags into typed request and
  response classes; `domain.py` holds typed ILS values; `rest/client.py` parses REST payloads
  into them once. Handlers see neither tags nor JSON.
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

## Sample connection flow

A complete terminal session, using real frames from a run against `docker compose`, with the
files and functions that handle each step and what, if anything, is sent to the ILS.

### 0. Before any device connects

Startup is in [`__main__.py`](cds_sip2/__main__.py).

1. `main()` calls `Settings.from_env()` in [`config.py`](cds_sip2/config.py). This takes the
   `CDS_SIP2_*` defaults, applies any environment variables with the same names, and validates the
   result. `CDS_SIP2_TERMINALS` is read here.
2. `serve()` builds the objects from the inside out:

   ```
   ILSClient (rest/client.py) → Handlers (sip2/handlers.py) → Dispatcher (sip2/dispatcher.py) → Sip2Server (sip2/server.py)
   ```

   The `Handlers` only ever see `ils` as an `ILSBackend` (the interface in
   [`domain.py`](cds_sip2/domain.py)). They never deal with HTTP directly.
3. `Sip2Server.start()` starts listening on port 3004.

### 1. The tablet connects

- `Sip2Server._accept()` ([`server.py`](cds_sip2/sip2/server.py)) refuses the connection if the
  server is shutting down or `max_connections` is reached. Otherwise it creates a new, empty
  `Session` ([`session.py`](cds_sip2/sip2/session.py)) for this connection.
- `_serve()` loops until the connection ends:
  1. `read_frame()` ([`framing.py`](cds_sip2/sip2/framing.py)) reads bytes up to the next `\r`. If
     nothing arrives within `idle_timeout` (300 s), the connection closes.
  2. The frame goes to `dispatcher.dispatch(frame, session)`.
  3. The reply bytes are written back, and the write must complete within `write_timeout` (5 s).

Every message below goes through the same `dispatch()` path, so it is described once here.

#### What `Dispatcher.dispatch()` does with every frame

See [`dispatcher.py`](cds_sip2/sip2/dispatcher.py).

1. **Log the request.** `_log("request", ...)` uses `codec.redact()`, which masks the password and
   any tag not in `LOGGABLE_TAGS`.
2. **Decode it.** `codec.decode()` ([`codec.py`](cds_sip2/sip2/codec.py)) works in four steps:
   - `checksum.split()` separates off the `AY<seq>AZ<checksum>` trailer and verifies it. A wrong
     checksum gets a `96` ("please resend").
   - The fixed-width part is sliced using the layout in [`messages.py`](cds_sip2/sip2/messages.py).
   - The remaining text is split on `|` into tagged fields.
   - The result is a `Request(code, fixed, fields, sequence)`.
3. **Handle resends.** A `97` replays the session's last reply. A frame identical to the previous
   one also gets the cached reply, if it was marked replayable, without calling the ILS again.
4. **Call `_route()`:**
   - Before login, anything except `93` or `99` is refused with the `not_logged_in` message.
   - It looks the code up in `handlers.routes`. Anything that isn't there, such as `63`, is
     refused as `unsupported`.
   - `transaction_id(request, terminal)` hashes the request's content, so a retried request gets
     the same ID.
   - `session.caller(txid)` builds `Caller(terminal, user_pid, transaction_id)`.
   - `mapper.parse(request)` turns the raw `Request` into its typed class, such as
     `ItemInformationRequest`.
   - The handler runs inside `asyncio.timeout(request_deadline)` (12 s).
5. **Send the reply.** `_encode()` runs `codec.encode()`, which adds the checksum trailer. Then
   `session.remember()` caches the reply for a possible `97`.

### 2. Login (93): no call to the ILS

```
>> 9300CNkiosk1|COsecret|CP|AY0AZF5CD
<< 941AY0AZFDFD
```

| Step | Where |
|---|---|
| Typed view: `LoginRequest(terminal="kiosk1", password="secret", location="")` | [`mapper.py`](cds_sip2/sip2/mapper.py) |
| `Handlers.login()` | [`handlers.py`](cds_sip2/sip2/handlers.py) |

`login()` works like this:

1. It calls `session.logout()` first, so a re-login always starts clean.
2. It looks up `settings.terminals["kiosk1"]`, which comes from `CDS_SIP2_TERMINALS`.
3. It checks the password with `hmac.compare_digest`. An unknown terminal is compared against a
   dummy password, so the response time doesn't reveal which terminal names exist.
4. On a match, `session.login(terminal, user_pid=entry["user_pid"], ...)` stores who this
   connection acts as. From now on `session.authenticated` is `True`.
5. It returns `LoginResponse(ok=True).reply()`. `to_response()` turns that into the wire message
   `94` with `ok=1`.

After 3 failed attempts (`max_login_attempts`), `dispatch()` closes the connection.

### 3. Status check (99): one `GET /ping`

```
>> 9900402.00AY1AZFCA4
<< 98YYYNNN150003...|AOCERN|AMCERN Library|BXNYYNYYYNNNYYNNNN|...
```

1. `Handlers.sc_status()` calls `self.ils.ping()`.
2. `ILSClient.ping()` ([`client.py`](cds_sip2/rest/client.py)) sends `GET /api/sip2/ping`. It
   returns `True` or `False` and never raises.
3. `_acs_status()` fills an `ACSStatus` response with:
   - online yes or no;
   - checkin and checkout allowed;
   - timeout `150` (15.0 s);
   - `BX`, the list of supported messages, derived from `handlers.routes`.

The tablet uses `BX` to decide what it is allowed to send.

### 4. Stocktake

A stocktake wand normally repeats two steps for every item it scans.

#### 4a. Look the item up (17): `GET /items/<barcode>`

```
>> 17...AOCERN|ABCM-B000456412|AY2AZF4F6
<< 18030001...CF0|ABCM-B000456412|AJÉcoute|BGCERN Library|CK001|AQMain reading room|APMain reading room|...
```

| Step | Where |
|---|---|
| `ItemInformationRequest(item_barcode="CM-B000456412")`; the barcode is normalised by `normalize_barcode` | [`mapper.py`](cds_sip2/sip2/mapper.py) |
| `Handlers.item_information()` | [`handlers.py`](cds_sip2/sip2/handlers.py) |
| `ILSClient.get_item(barcode)` sends `GET /api/sip2/items/CM-B000456412` with `Authorization: Bearer <CDS_SIP2_ILS_TOKEN>` | [`client.py`](cds_sip2/rest/client.py) |

1. The ILS answers with JSON, for example
   `{"title": "Écoute", "status": "available", "location": "Main reading room", "media_type": "book", ...}`.
2. `_item(payload)` in [`client.py`](cds_sip2/rest/client.py) parses that, once, into a
   `domain.Item`:
   - `_item_status("available")` gives `ItemStatus.AVAILABLE`. An unknown status becomes `OTHER`,
     and a warning is logged.
   - `_date()` and `_int()` parse their fields; a bad value is dropped with a warning.
3. The handler translates the domain values into SIP2 codes using the maps in
   [`enums.py`](cds_sip2/sip2/enums.py):
   - `CIRCULATION_STATUS[ItemStatus.AVAILABLE]` gives `"03"`;
   - `MEDIA_TYPE[MediaType.BOOK]` gives `"001"`;
   - the security marker comes from settings.
4. It fills `ItemInformationResponse(title=item.title, permanent_location=item.location, ...)`.
   The tablet sees status `03` (available) and title `Écoute`.

Possible outcomes:

- **ILS returns 404:** `get_item` returns `None`, and the handler refuses with the `unknown_item`
  message.
- **ILS is down:** `ILSUnavailable`, and the handler refuses with `unavailable`.

#### 4b. Record the item as seen (19): `POST /items/<barcode>/status`

```
>> 19...AOCERN|ABCM-B000456412|AC|CHshelf-1|AY3AZF07C
<< 201...ABCM-B000456412|AJÉcoute|AFSeen: shelf-1|...
```

| Step | Where |
|---|---|
| `ItemStatusUpdateRequest(item_barcode=..., properties="shelf-1")`; `CH` is the "item properties" field | [`mapper.py`](cds_sip2/sip2/mapper.py) |
| `Handlers.item_status_update()` runs the call through `_write()` | [`handlers.py`](cds_sip2/sip2/handlers.py) |
| `ILSClient.update_item_status(item_barcode, properties, caller)` goes through `_write()`, then `_request()` | [`client.py`](cds_sip2/rest/client.py) |

The ILS receives the request below. `_write` adds the three `Caller` fields on its own:

```
POST /api/sip2/items/CM-B000456412/status
{"item_barcode": "CM-B000456412", "properties": "shelf-1",
 "user_pid": "42", "terminal": "kiosk1", "transaction_id": "<32 hex>"}
```

- The ILS answers with the item after the update. `_item()` parses it, and the handler returns
  `ItemStatusUpdateResponse(item_properties_ok=True, title=...)`.
- What "shelf-1" means is up to the ILS. The SIP2 server passes `CH` through unchanged; see open
  question 5 below.

#### What happens when a write fails

This applies to 11, 09 and 19. `_request()` in [`client.py`](cds_sip2/rest/client.py) turns each
failure into a typed error from [`errors.py`](cds_sip2/errors.py), and `Handlers._write()` turns
that into a refusal:

| What happened | Error | What the tablet gets |
|---|---|---|
| ILS returned 404 | `ILSNotFound` | "unknown item" refusal |
| ILS returned 409 or another 4xx, or `{"ok": false}` | `ILSRejected` | the ILS's own message |
| Couldn't connect, so nothing was sent | `ILSUnavailable` | "unavailable"; safe to retry |
| Sent, then timed out, got a 5xx, or got a garbled answer | `ILSAmbiguous` | "ambiguous", marked `replayable=False` |
| Took longer than 12 s | dispatcher deadline | "ambiguous", marked `replayable=False` |

`replayable=False` means that when the tablet resends, the server asks the ILS again with the same
`transaction_id` rather than replaying the cached reply. The ILS must recognise that ID and answer
with the original result, so the update is never applied twice.

### 5. Checkout and checkin (11, 09), if the device does them

These follow the same pattern as 19:

- `Handlers.checkout()` calls `ILSClient.checkout()`, which sends `POST /checkout` with the item
  barcode, the patron barcode and the caller fields. The answer is parsed by `_checkout_result()`
  into a `CheckoutResult`.
- `Handlers.checkin()` calls `ILSClient.checkin()`, which sends `POST /checkin`. The answer is
  parsed by `_checkin_result()` into a `CheckinResult`. If `alert` is set, the device beeps, for
  example when the item has a hold waiting.

### 6. Logout: there is no message for it

SIP2 2.00 has no message for a terminal to log out. (`35` "End Patron Session" ends a *patron's*
session, not the terminal's, and it is unsupported here.) In practice a terminal's session ends in
one of three ways:

- **The tablet closes the TCP connection.** `read_frame()` raises `ConnectionClosed`, `_serve()`
  returns, and `_accept()` logs `connection closed` and closes the socket. The `Session` is simply
  discarded; nothing is stored anywhere. No call goes to the ILS, because the ILS was never told
  about the login.
- **The connection is idle for 300 s.** The server closes it.
- **The tablet sends a new `93`.** `login()` calls `session.logout()` first, clearing the terminal
  and `user_pid`, and then logs in again.

On the next connection the tablet must log in again, because sessions are never shared between
connections.

### The whole flow on one page

```
tablet ──TCP──▶ server._serve ─▶ framing.read_frame ─▶ dispatcher.dispatch
                                                          │ codec.decode (+checksum)
                                                          │ 97 / resend cache
                                                          │ _route: login gate, transaction_id, session.caller
                                                          │ mapper.parse ─▶ typed request
                                                          ▼
                                     handlers.<message>(message, session, caller)
                                                          │ (93: settings.terminals only)
                                                          ▼
                                     rest/client.ILSClient ── HTTPS + Bearer ──▶ ILS
                                                          │ _item / _checkout_result ... → domain types
                                                          ▼
                                     mapper.<X>Response ─▶ codec.encode ─▶ session.remember ─▶ tablet
```

To watch this live while the tablet runs, use `docker compose logs -f sip2`. Each request and
response is logged as a `sip2.request` / `sip2.response` event, and each ILS outcome is logged
under its handler's name, such as `item_information` or `item_status_update`.

## Open questions

1. **Resend behaviour of the real hardware.** The transaction id assumes a kiosk resends with the
   same sequence number and transaction date. If the hardware regenerates either, resends will
   not be deduplicated. This needs testing with the actual devices.
2. **Duplicate window.** How long the ILS honours a duplicate `transaction_id` must be agreed and
   written into the contract above.
3. **Timeouts.** The ILS timeouts (3 seconds to connect, 8 to read), `CDS_SIP2_TIMEOUT_PERIOD`
   and `CDS_SIP2_RETRIES_ALLOWED` should be tuned against the hardware. The 98 timeout field is
   advertised in tenths of a second, as SIP2 2.00 specifies; confirm the devices honour it.
4. **Readiness during an ILS outage.** With one replica, an unready pod means kiosks cannot
   connect at all rather than seeing "unavailable" in a 98. Decide whether readiness should
   track the ILS.
5. **Item status update semantics.** What the ILS should do with the `CH` value of a 19 (record
   "seen", change a location, ...) must be agreed for the inventory wands.
6. **Terminal password storage.** `CDS_SIP2_TERMINALS` holds plaintext passwords (in a Secret).
   Hashing them is possible if the Secret is considered too exposed.

## Known limitations

- **SIP2 is unencrypted by nature.** TLS is terminated by the stunnel sidecar. Without it,
  passwords and PINs are transmitted in clear text.
- **Per-IP limits must be applied at the edge.** Behind stunnel the server sees every connection
  as coming from loopback; it enforces only a global connection limit and per-connection login
  attempts. The PROXY protocol, or terminating TLS in-process with client certificates, would
  identify kiosks by address or certificate.
- **Due-date times use the container's timezone**, which is typically UTC.
- **Screen messages are available in English only**, although the language is configurable.
- **Patron messages and renewals are not supported yet.** They are refused as unsupported.

## Configuration

Every setting is a module-level `CDS_SIP2_*` variable in `cds_sip2/config.py` (with its default
and documentation), overridable by an environment variable of the same name. Mappings are given
as JSON objects. An invalid value stops the service at start-up with exit code 2.

| Variable | Default | Notes |
|---|---|---|
| `CDS_SIP2_ILS_URL` | `http://localhost:5000/api/sip2` | Base URL of the ILS API |
| `CDS_SIP2_ILS_TOKEN` | empty | Bearer token for the ILS. Must be supplied from a secret. |
| `CDS_SIP2_ILS_CONNECT_TIMEOUT`, `CDS_SIP2_ILS_READ_TIMEOUT` | `3`, `8` (seconds) | Per-operation httpx limits |
| `CDS_SIP2_TERMINALS` | `{}` | JSON `{"<CN>": {"password": "...", "user_pid": "..."}}`. Must be supplied from a secret. Empty refuses every login. |
| `CDS_SIP2_BIND_HOST`, `CDS_SIP2_PORT` | `127.0.0.1`, `3004` | SIP2 listener; loopback behind stunnel |
| `CDS_SIP2_MAX_CONNECTIONS` | `50` | Further connections are closed immediately |
| `CDS_SIP2_IDLE_TIMEOUT` | `300` (seconds) | Time without a complete frame before the connection is closed |
| `CDS_SIP2_WRITE_TIMEOUT` | `5` (seconds) | Time a reply may take to drain before the connection is closed |
| `CDS_SIP2_MAX_FRAME_BYTES` | `4096` | Longer input closes the connection |
| `CDS_SIP2_SHUTDOWN_GRACE` | `10` (seconds) | Time in-flight requests may take after SIGTERM |
| `CDS_SIP2_ERROR_DETECTION` | `true` | Verify AZ on requests, emit AY/AZ on responses |
| `CDS_SIP2_REQUIRE_CHECKSUM` | `false` | With error detection on, also answer `96` to requests without a trailer |
| `CDS_SIP2_ENCODING` | `utf-8` | Text encoding on the wire |
| `CDS_SIP2_REQUEST_DEADLINE` | `12` (seconds) | Overall bound on one request; must be below `CDS_SIP2_TIMEOUT_PERIOD` |
| `CDS_SIP2_TIMEOUT_PERIOD` | `15` (seconds) | Advertised in the 98 in tenths of a second (`150`); at most 99.9 |
| `CDS_SIP2_RETRIES_ALLOWED` | `3` | Advertised in the 98 |
| `CDS_SIP2_MAX_LOGIN_ATTEMPTS` | `3` | Failed 93s before the connection is closed |
| `CDS_SIP2_DEFAULT_MEDIA_TYPE` | `001` (book) | SIP2 `CK` code for items without a media type |
| `CDS_SIP2_SECURITY_MARKER` | `00` (other, RFID) | SIP2 security marker sent for every item |
| `CDS_SIP2_INSTITUTION_ID`, `CDS_SIP2_LIBRARY_NAME`, `CDS_SIP2_LANGUAGE` | `CERN`, `CERN Library`, `en` | Session values after login |
| `CDS_SIP2_MESSAGES` | see `config.py` | JSON object of screen messages, merged over the defaults |
| `CDS_SIP2_HEALTH_HOST`, `CDS_SIP2_HEALTH_PORT` | `0.0.0.0`, `8080` | Health listener; must be reachable by the kubelet |
| `CDS_SIP2_HEALTH_ILS_CACHE` | `10` (seconds) | How long `/readyz` reuses an ILS ping |
| `CDS_SIP2_LOG_LEVEL` | `INFO` | Secrets are masked at every level, including `DEBUG` |
| `CDS_SIP2_LOG_HASH_KEY` | random per process | Key for patron pseudonyms. Set from a secret. |

The invenio-era variables (`SIP2_DATASTORE_REDIS_URL`, `SIP2_TIMEOUT_PERIOD`, `SIP2_PORT`,
`SIP2_BIND_HOST`, `SECRET_KEY`, `INVENIO_*`, ...) are no longer read.
