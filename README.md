# cds-sip2

A SIP2 gateway for CDS-ILS.

Self-check kiosks and RFID hardware in libraries communicate using a protocol called SIP2, whereas
CDS-ILS exposes an HTTP interface. This project bridges the two. It runs the SIP2 socket server
provided by [invenio-sip2](https://invenio-sip2.readthedocs.io/) and translates each incoming
SIP2 message into a REST call against a dedicated `/api/sip2` API on the ILS.

The service intentionally contains no circulation logic. It does not decide who may borrow an
item, for how long, or whether a patron is blocked. Those decisions belong to the ILS, and the
service's only responsibility is to relay the ILS's answer in SIP2 form.

## Status

> **Draft, published for discussion of the approach and structure.**
>
> - [The `/api/sip2` endpoints on CDS-ILS do not yet exist. The contract described below is a
>   proposal, and `tests/fake_ils.py` is currently its only implementation.]
> - The service has not been deployed to any environment.

## Architecture

```
 kiosk / RFID reader
        │  SIP2 over TCP
        ▼
 stunnel (TLS termination)          yet to be implemented
        │  plaintext SIP2 on 127.0.0.1:3004
        ▼
 cds-sip2 (this repository) ─────►  Redis (SIP2 session state)
        │  HTTPS with bearer token
        ▼
 CDS-ILS  /api/sip2/*
```

SIP2 is a line-based protocol in which the
device sends a two-digit message code followed by a series of fields (for example `93` for login,
`63` for a patron information request, and `11` for a checkout). The server replies with the
corresponding response code. The invenio-sip2 documentation provides a good overview, and the
3M SIP2 specification remains the authoritative reference.

## Repository layout

| Path | Purpose |
|---|---|
| `cds_sip2/config.py` | All `SIP2_*` and `CDS_SIP2_*` settings, together with the mapping from SIP2 actions to handlers. Loaded through the `invenio_config.module` entry point. |
| `cds_sip2/ext.py` | A minimal Flask extension that creates the shared `ILSClient`. Registered through the `invenio_base.apps` entry point. |
| `cds_sip2/handlers.py` | The callbacks invoked by invenio-sip2 for each SIP2 message. |
| `cds_sip2/ils_client.py` | The HTTP client for the ILS. Its docstrings also serve as the written contract. |
| `cds_sip2/permissions.py` | Permission factory for the invenio-sip2 monitoring views. All requests are denied unless a monitoring token is configured. |
| `cds_sip2/utils.py` | Barcode normalisation and SIP2 date formatting. |
| `tests/sip2_client.py` | A scripted SIP2 client that depends only on the standard library. It can also be used as a command-line tool against a deployed server. |
| `tests/fake_ils.py` | An in-memory implementation of the ILS API, used for tests and local development. |
| `Dockerfile`, `entrypoint.sh` | The container image and its entrypoint (`server`, `healthcheck`, or an arbitrary command). |
| `docker-compose.yml` | A local stack comprising Redis, the fake ILS and the service, without TLS. |
| `pyproject.toml`, `requirements.in`, `requirements.txt` | Packaging. `pyproject.toml` declares loose version ranges, and `requirements.txt` holds the pinned set used for deployment. |

## Running the service locally

Docker is required. Python is needed only to run the tests or the client outside a container.

```bash
docker compose up --build -d
```

This starts Redis, a fake ILS on port 5001, and the service on port 3004. The ports are published
on all interfaces for convenience and the fake credentials are publicly known, so the stack
should be run on a personal workstation rather than a shared host.

In a second terminal, a scripted session can then be run against it:

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

To follow the service logs without the health-check entries, use
`docker compose logs -f sip2 | grep -v 127.0.0.1`. To stop the stack, use `docker compose down`.

## Running the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -e ".[tests]"   # -e registers the entry points
./run-tests.sh
```

- `unit/test_ils_client.py` covers the HTTP contract, timeouts, error mapping, and the absence of retries.
- `unit/test_handlers.py` exercises each handler against a mocked ILS and checks the model properties that invenio-sip2 reads.
- `unit/test_sip2_client.py` verifies the framing, checksum and parsing logic of the test client.
- `integration/test_sip2_protocol.py` starts the real socket server with the fake ILS and a live
  Redis instance, then drives it over TCP. It covers login, the 98 capabilities response, a full
  session including a refused checkout, unknown patrons and items, a blocked patron, a 97 resend,
  and checksum validation on every response.

## Supported messages

| Request | Response | Handler and ILS call |
|---|---|---|
| 93 Login | 94 | `login_handler` → `POST /login` |
| 99 SC Status | 98 ACS Status | `system_status_handler` → `GET /ping`. The capability flags are taken from the `SIP2_SUPPORT_*` settings. |
| 23 Patron Status | 24 | `patron_status` → `GET /patrons/{barcode}` |
| 25 Patron Enable | 26 | `enable_patron` → `GET /patrons/{barcode}`. Reports the current status; it cannot lift a block. |
| 63 Patron Information | 64 | `account` → `GET /patrons/{barcode}`, and `authorize_patron` → `POST /patrons/{barcode}/verify` when the device supplies a PIN |
| 17 Item Information | 18 | `item` → `GET /items/{barcode}` |
| 11 Checkout | 12 | `checkout` → `POST /checkout` |
| 09 Checkin | 10 | `checkin` → `POST /checkin` |
| 29 Renew | 30 | `renew` → `POST /renew` |
| 35 End Patron Session | 36 | None. The session is cleared in Redis. |
| 97 Request ACS Resend | Last response | None |

Hold (15), Renew All (65), Fee Paid (37) and Item Status Update (19) are intentionally not
configured. invenio-sip2 therefore advertises them as unsupported in the `BX` field of the 98
response.

## ILS API contract

All requests carry the header `Authorization: Bearer <CDS_SIP2_ILS_TOKEN>`. The ILS maintains the
terminal registry, which associates each terminal with its password, acting user and location, so
the service never needs to know about locations. Each circulation request passes `terminal` and
`user_pid` for this purpose.

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

The service interprets responses as follows.

- **Not found.** A `404` on a patron or item lookup means the patron or item is unknown. The
  service converts this into a normal "invalid patron" or "unknown item" SIP2 reply.
- **Refusals.** A `409` (or another `4xx`) on a circulation call means the ILS has declined the
  transaction. The body has the form `{"code", "message", "user_message", "title"}`, and the
  `user_message` is displayed on the kiosk screen.
- **Invalid service token.** A `401` or `403` indicates that the service's own credentials are
  wrong. It is logged as a configuration error, and the kiosk displays a generic "unavailable"
  message.
- **Outages.** A `5xx` response or a timeout also results in a generic "unavailable" message.
  Requests are never retried, because retrying a checkout could record the loan twice.
- **Patron status flags** are values of `invenio_sip2.models.PatronStatusTypes`, for example
  `charge_privileges_denied` or `excessive_outstanding_fines`.
- **Item status** is one of `available`, `on_loan`, `recalled`, `on_hold_shelf`, `in_transit`,
  `in_process`, `on_order`, `lost`, `missing` or `other`.
- **Dates** use ISO-8601. A date without a time is presented to the kiosk as the end of that day.

## Design decisions

The following choices are those on which feedback is most welcome.

- **A separate service rather than a component of the ILS.** SIP2 is a long-lived socket protocol
  with its own operational concerns, such as TLS termination, per-device sessions and
  hardware-specific behaviour. Keeping it outside the ILS allows it to be deployed, scaled and
  diagnosed independently. [Add any further reasons.]
- **No circulation logic in the service.** Loan periods, blocks, renewals and holds are all
  determined by the ILS. The handlers only translate between formats, which is also why the ILS
  owns the terminal registry.
- **invenio-sip2 as the foundation.** It already provides the socket server, message framing,
  checksums and session storage, leaving only the handlers to be written. [State the pinned
  version and the reason for choosing it.]
- **Distinct treatment of refusals and outages.** When the ILS declines a transaction, the kiosk
  receives a clean refusal containing the ILS's own message. When the ILS is unreachable, the
  kiosk receives a generic "service unavailable" message and the log records a more prominent
  entry. In both cases the device receives a well-formed SIP2 reply rather than a dropped connection.
- **Redis for session state.** invenio-sip2 stores per-device sessions there, including the
  authenticated terminal and its acting user.

## Open questions

1. **Idempotency.** Each POST carries a `transaction_id` so that the ILS can ignore duplicates.
   However, the service currently generates a new identifier for every handler call, so a frame
   resent by the kiosk after a timeout is indistinguishable from a new transaction. Should the
   identifier instead be derived from the terminal and the SIP2 sequence number?
2. **Ambiguous outcomes.** If a checkout times out while awaiting the ILS, the transaction may or
   may not have been recorded. At present this is treated identically to the ILS being
   unavailable. It likely warrants a distinct error and a distinct kiosk message, such as a
   request to check at the desk.
3. **Timeouts and retries.** The ILS timeouts (3 seconds to connect, 8 to read) and the
   `SIP2_RETRIES_ALLOWED` setting should be tuned against the behaviour of the actual hardware
   when a reply is slow.
4. **Status code for unknown items on circulation calls.** The contract reserves `404` for
   lookups and uses `409` for refusals, but the fake ILS currently returns `404` for an unknown
   item on checkout, checkin and renew. The preferred behaviour should be agreed, and the client
   and fake aligned with it.
5. **Entry point.** The extension is registered under `invenio_base.apps`, which is believed to be
   the application loaded by the `invenio selfcheck` command. A test confirming this would be valuable.

## Known limitations

- **Logs may contain sensitive data.** invenio-sip2 logs every request and response frame at INFO
  level. This includes patron barcodes and probably patron PINs (`AD`) and terminal passwords
  (`CO`). What actually reaches the console and log file has not yet been verified. [Update once
  checked.] The service must never be run with `SIP2_LOGGING_CONSOLE_LEVEL=DEBUG` in production.
- **SIP2 is unencrypted by nature.** TLS is expected to be terminated by stunnel in front of the
  service. Without it, passwords and PINs are transmitted in clear text.
- **Free text is not sanitised.** A title or name containing `|` would corrupt the SIP2 frame.
- **Due-date times use the container's timezone**, which is typically UTC.
- **Screen messages are available in English only**, although the language is configurable.
- **All items are reported as books.** Security markers are not used, since the hardware relies on RFID.
- **The health check is shallow.** It confirms only that the port accepts connections, not that
  Redis or the ILS is healthy.

## Configuration

All settings are supplied through environment variables. Any other Invenio setting may be provided
as `INVENIO_<NAME>`.

| Variable | Default | Notes |
|---|---|---|
| `CDS_SIP2_ILS_URL` | `http://localhost:5000/api/sip2` | Base URL of the ILS API |
| `CDS_SIP2_ILS_TOKEN` | empty | Bearer token for the ILS. Must be supplied from a secret. |
| `CDS_SIP2_ILS_CONNECT_TIMEOUT`, `CDS_SIP2_ILS_READ_TIMEOUT` | `3`, `8` (seconds) | Should remain well below `SIP2_TIMEOUT_PERIOD` |
| `CDS_SIP2_INSTITUTION_ID`, `CDS_SIP2_LIBRARY_NAME`, `CDS_SIP2_LANGUAGE` | `CERN`, `CERN Library`, `en` | Fallbacks used when the ILS login response omits them |
| `CDS_SIP2_MONITORING_TOKEN` | unset | Enables the invenio-sip2 monitoring views through the `X-SIP2-Monitoring-Token` header. When unset, they deny all requests. |
| `SIP2_DATASTORE_REDIS_URL` | `redis://localhost:6379/0` | Location of the session store |
| `SIP2_DATASTORE_REDIS_PREFIX` | `cds_sip2` | Key prefix in Redis |
| `SIP2_TIMEOUT_PERIOD`, `SIP2_RETRIES_ALLOWED` | `15`, `3` | Advertised to the kiosk in the 98 response |
| `SIP2_LOGGING_CONSOLE_LEVEL` | `INFO` | Must never be `DEBUG` in production |
| `CDS_SIP2_LOG_FILE` | unset (the image sets `/var/log/sip2/sip2.log`) | Rotating log of requests and responses |
| `SIP2_SERVER_NAME`, `SIP2_BIND_HOST`, `SIP2_PORT`, `SIP2_REMOTE_APP` | `cds`, `127.0.0.1`, `3004`, `ils` | Read by `entrypoint.sh` |
| `SECRET_KEY`, `SQLALCHEMY_DATABASE_URI` | development value, `sqlite://` | Inert placeholders that satisfy Invenio extensions pulled in indirectly. A real `SECRET_KEY` should still be set. |
