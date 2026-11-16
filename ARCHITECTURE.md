# How the code fits together

A short tour for anyone opening this repository for the first time. For
configuration, protocol behaviour and deployment, see the [README](README.md).

## The big picture

```
kiosk ──TLS──▶ stunnel sidecar ──TCP──▶ SIP2 server (this repo) ──HTTPS──▶ ILS REST API
```

The server speaks SIP2 to self-check kiosks and turns each request into a REST
call to the ILS. The code is split into two packages that never import each
other:

- **`cds_sip2/sip2/`** knows the SIP2 protocol: bytes, frames, messages and
  sessions. It knows nothing about HTTP.
- **`cds_sip2/ils/`** knows the ILS HTTP API. It knows nothing about SIP2.

They meet in exactly one place, [`__main__.py`](cds_sip2/__main__.py), which
hands the ILS client to the SIP2 side. The SIP2 side only ever sees the
`ILSBackend` interface, so tests can swap in a fake.

```
cds_sip2/
├── __main__.py      start-up: wires everything together and runs it
├── config.py        Settings, read once from CDS_SIP2_* environment variables
├── logging.py       JSON logs to stdout; pseudonymises patron barcodes
├── health.py        /livez and /readyz HTTP endpoints for OpenShift
├── utils.py         small helpers (dates, barcode normalisation)
├── ils/
│   ├── interface.py ILSBackend: the methods the SIP2 side may call
│   ├── client.py    ILSClient: the httpx implementation of ILSBackend
│   └── errors.py    ILSUnavailable, ILSAmbiguous, ILSRejected, ...
└── sip2/
    ├── server.py    asyncio TCP server: accepts connections, reads frames
    ├── framing.py   splits the byte stream into CR-terminated frames
    ├── checksum.py  the AY/AZ error-detection trailer
    ├── codec.py     bytes ⇄ Request / Response objects; log redaction
    ├── messages.py  the field layout of every SIP2 message
    ├── enums.py     SIP2 code tables (item status, patron flags, ...)
    ├── session.py   per-connection state (terminal, login, last reply)
    ├── dispatcher.py the rules that apply to every message
    └── handlers.py  one method per message: what checkout, checkin, ... do
```

## From start-up to a running server

### 1. Process start

`entrypoint.sh server` (the container's default) runs `exec python -m cds_sip2`.
The `exec` matters: the orchestrator's SIGTERM reaches Python directly.

### 2. `__main__.main()`: settings and logging

- `Settings.from_env()` in [config.py](cds_sip2/config.py) reads every
  `CDS_SIP2_*` variable into a frozen dataclass and validates it. A bad value
  prints an error and exits with code 2 before anything starts.
- `logging.configure()` in [logging.py](cds_sip2/logging.py) installs the JSON
  formatter on stdout and sets the key used to pseudonymise patron barcodes.
- `asyncio.run(serve(settings))` starts the event loop.

### 3. `__main__.serve()`: building the objects

The objects are built from the inside out:

```python
ils     = ILSClient(...)                 # ils/client.py
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
     has logged in
   - give the request a `transaction_id` derived from its content, so a retry
     sends the ILS the same id
3. **Handling.** The dispatcher calls `Handlers.checkout()` in
   [handlers.py](cds_sip2/sip2/handlers.py). This calls `ils.checkout(...)` with
   the item, the patron, the terminal's login and the transaction id.
4. **ILS call.** `ILSClient` in [client.py](cds_sip2/ils/client.py) makes the
   HTTP POST and turns each failure into a typed error from
   [errors.py](cds_sip2/ils/errors.py). If the request was never sent, it raises
   `ILSUnavailable`. If it was sent but no answer came back, it raises
   `ILSAmbiguous`: the loan may or may not exist.
5. **Reply.** The handler builds a `12` response with `codec.build()`. Errors
   become a refusal with a kiosk-friendly message from `config.messages`.
   Values from `enums.py` fill the fixed fields.
6. **Back out.** The dispatcher encodes the response, adds the checksum trailer,
   and stores it in the session for a later `97`. Ambiguous replies are not
   stored for a blind replay. The server writes the bytes back to the kiosk.

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
| add or alter a message's fields | `sip2/messages.py` |
| change a rule for all messages (login gating, resends) | `sip2/dispatcher.py` |
| change how the ILS is called or how its errors are mapped | `ils/client.py` |
| add a setting | `config.py` (then the README table) |
| change what gets logged or masked | `sip2/codec.py` (`redact`), `logging.py` |

## Tests

- `tests/conftest.py` provides `FakeILS`, an in-memory `ILSBackend`, so the unit
  tests in `tests/unit/` drive handlers, the dispatcher and the server without a
  network.
- `tests/integration/` starts the real server as a subprocess against
  `tests/fake_ils.py`, a small Flask app.
- `tests/sip2_client.py` is a command-line kiosk for trying the server by hand.
