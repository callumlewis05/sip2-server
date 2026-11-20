# How the code fits together

A short tour for anyone opening this repository for the first time. For
configuration, protocol behaviour and deployment, see the [README](README.md).

## The big picture

```
kiosk ──TLS──▶ stunnel sidecar ──TCP──▶ SIP2 server (this repo) ──HTTPS──▶ ILS REST API
```

The server speaks SIP2 to self-check kiosks and RFID wands and turns each
request into a REST call to the ILS. The code is split into two packages that
never import each other, plus the domain module they share:

- **`cds_sip2/domain.py`** is the ILS as the SIP2 side sees it: typed values
  (`Item`, `CheckoutResult`, `ItemStatus`, `Caller`, ...) and the
  `ILSBackend` Protocol (the port). No SIP2 tags, no HTTP.
- **`cds_sip2/sip2/`** knows the SIP2 protocol: bytes, frames, messages and
  sessions. It knows nothing about HTTP.
- **`cds_sip2/rest/`** is the adapter for the ILS REST API. It parses every
  payload into the domain types, once. It knows nothing about SIP2.

They meet in exactly one place, [`__main__.py`](cds_sip2/__main__.py), which
hands the REST client to the SIP2 side. The SIP2 side only ever sees the
`ILSBackend` Protocol, so tests can swap in a fake that returns the same
domain objects without HTTP.

Supported today: 93 Login, 99 SC Status, 17 Item Information, 11 Checkout,
09 Checkin and 19 Item Status Update. Every other known message gets a
well-formed "unsupported" refusal.

```
cds_sip2/
├── __main__.py      start-up: wires everything together and runs it
├── config.py        CDS_SIP2_* defaults as module variables; frozen Settings
├── domain.py        domain types and the ILSBackend Protocol
├── errors.py        ILSUnavailable, ILSAmbiguous, ILSRejected, ...
├── logging.py       JSON logs to stdout; pseudonymises patron barcodes
├── health.py        /livez and /readyz HTTP endpoints for OpenShift
├── utils.py         small helpers (dates, barcode normalisation)
├── rest/
│   └── client.py    ILSClient: the httpx adapter; parses payloads into domain types
└── sip2/
    ├── server.py    asyncio TCP server: accepts connections, reads frames
    ├── framing.py   splits the byte stream into CR-terminated frames
    ├── checksum.py  the AY/AZ error-detection trailer
    ├── codec.py     bytes ⇄ Request / Response wire objects; log redaction
    ├── messages.py  the field layout of every SIP2 message
    ├── mapper.py    typed request/response classes per message; refusals
    ├── enums.py     SIP2 code tables, and the maps from domain enums to them
    ├── session.py   per-connection state (terminal, login, last reply)
    ├── dispatcher.py the rules that apply to every message
    └── handlers.py  one method per message: what checkout, checkin, ... do
```

## From start-up to a running server

### 1. Process start

`entrypoint.sh server` (the container's default) runs `exec python -m cds_sip2`.
The `exec` matters: the orchestrator's SIGTERM reaches Python directly.

### 2. `__main__.main()`: settings and logging

- `Settings.from_env()` in [config.py](cds_sip2/config.py) takes the
  module-level `CDS_SIP2_*` defaults, applies environment overrides of the
  same name, and validates the resulting frozen object. A bad value
  prints an error and exits with code 2 before anything starts.
- `logging.configure()` in [logging.py](cds_sip2/logging.py) installs the JSON
  formatter on stdout and sets the key used to pseudonymise patron barcodes.
- `asyncio.run(serve(settings))` starts the event loop.

### 3. `__main__.serve()`: building the objects

The objects are built from the inside out:

```python
ils     = ILSClient(...)                 # rest/client.py
handlers = Handlers(ils, settings)       # sip2/handlers.py
dispatcher = Dispatcher(handlers, settings)  # sip2/dispatcher.py
server  = Sip2Server(dispatcher, settings)   # sip2/server.py
health  = HealthServer(server, ILSProbe(ils, ...), settings)  # health.py
```

`serve()` then installs SIGTERM and SIGINT handlers, starts the SIP2 listener
(default port 3004) and the health listener (default port 8080), and waits.

### 4. A kiosk connects

[`Sip2Server._accept`](cds_sip2/sip2/server.py) refuses the connection if the
server is draining or at `max_connections`. Otherwise it turns on TCP
keepalive, creates a fresh [`Session`](cds_sip2/sip2/session.py) for the
connection, and starts the `_serve` loop.

### 5. One request, end to end

Take a checkout (`11`) as an example:

1. **Framing.** `_serve` calls `read_frame()` in [framing.py](cds_sip2/sip2/framing.py),
   which reads up to the next `\r`. A frame larger than `max_frame_bytes` or an
   idle connection closes the connection.
2. **Dispatch.** The frame goes to `Dispatcher.dispatch()` in
   [dispatcher.py](cds_sip2/sip2/dispatcher.py), which applies the rules every
   message shares:
   - log the request with secrets masked (`codec.redact`)
   - decode it with `codec.decode()`, which uses `checksum.py` and the layouts in
     `messages.py`; a bad checksum is answered with `96` (please resend)
   - answer `97` (resend your last reply) from the session's cache
   - if the kiosk resends the exact same frame, replay the cached reply instead
     of repeating the checkout
   - refuse everything except login (`93`) and status (`99`) until the terminal
     has logged in (93 is checked against `CDS_SIP2_TERMINALS`, not the ILS)
   - derive a `transaction_id` from the request's content, so a retry sends the
     ILS the same id, and wrap it with the session's terminal and `user_pid`
     in a `Caller`
   - parse the request into its typed view (`mapper.CheckoutRequest`)
   - run the handler within `request_deadline` seconds
3. **Handling.** The dispatcher calls `Handlers.checkout()` in
   [handlers.py](cds_sip2/sip2/handlers.py). This calls
   `ils.checkout(item_barcode=..., patron_barcode=..., caller=...)`.
4. **ILS call.** `ILSClient` in [client.py](cds_sip2/rest/client.py) makes the
   HTTP POST, parses the answer into a `CheckoutResult`, and turns each failure
   into a typed error from [errors.py](cds_sip2/errors.py). If the request was
   never sent, it raises `ILSUnavailable`. If it was sent but no usable answer
   came back, it raises `ILSAmbiguous`: the loan may or may not exist.
5. **Reply.** The handler fills a `mapper.CheckoutResponse` from the result and
   returns it as a `Reply`. Errors become a refusal (`Handlers.refuse`, which
   uses the response class's refused-state defaults) with a kiosk-friendly
   message from `settings.messages`. `enums.py` maps the domain values onto
   SIP2 codes.
6. **Back out.** The dispatcher encodes the response, adds the checksum trailer,
   and stores it in the session for a later `97`. Replies marked
   `replayable=False` (ambiguous outcomes, deadline expiry, crashes) are not
   used for a blind replay. The server writes the bytes back to the kiosk
   within `write_timeout` seconds.

### 6. Health checks

The [`HealthServer`](cds_sip2/health.py) answers:

- `/livez` with 200 while the SIP2 listener is up.
- `/readyz` with 200 when the listener is up and an ILS ping succeeds. The ping
  result is cached for a few seconds.

### 7. Shutdown

On SIGTERM, `serve()` wakes and calls `Sip2Server.shutdown()`. It stops
accepting new connections and closes idle ones straight away. Any connection in
the middle of a request gets `shutdown_grace` seconds to finish before it is
cancelled. Then the health server and the HTTP client close, and the process
exits 0.

## Where to look when…

| You want to… | Start in |
|---|---|
| change what a message does | `sip2/handlers.py` |
| support a new message | a request/response class in `sip2/mapper.py`, a route in `sip2/handlers.py` |
| add or alter a message's fields | `sip2/messages.py`, then `sip2/mapper.py` |
| change a rule for all messages (login gating, resends, deadline) | `sip2/dispatcher.py` |
| change the ILS vocabulary (statuses, media types) | `domain.py`, then the maps in `sip2/enums.py` |
| change how the ILS is called, parsed, or its errors mapped | `rest/client.py` |
| add a setting | a `CDS_SIP2_*` variable in `config.py` (then the README table) |
| change what gets logged or masked | `sip2/codec.py` (`LOGGABLE_TAGS`, `redact`), `logging.py` |

## Tests

- `tests/conftest.py` provides `FakeILS`, an in-memory `ILSBackend`, so the unit
  tests in `tests/unit/` drive handlers, the dispatcher and the server without a
  network.
- `tests/integration/` starts the real server as a subprocess against
  `tests/fake_ils.py`, a small Flask app.
- `tests/sip2_client.py` is a command-line kiosk for trying the server by hand.
