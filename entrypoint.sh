#!/bin/sh
# Entrypoint for the CDS SIP2 sidecar.
#
#   server       start the SIP2 socket server (default)
#   healthcheck  exit 0 if the server accepts TCP connections (use as exec probe)
#   <anything>   exec it as-is (`sh`, `invenio shell`, ...)
#
# Environment (defaults match the Dockerfile):
#   SIP2_SERVER_NAME  name of the server record in Redis          (cds)
#   SIP2_BIND_HOST    bind address; keep 127.0.0.1 behind stunnel (127.0.0.1)
#   SIP2_PORT         TCP port                                    (3004)
#   SIP2_REMOTE_APP   key in SIP2_REMOTE_ACTION_HANDLERS          (ils)
set -eu

: "${SIP2_SERVER_NAME:=cds}"
: "${SIP2_BIND_HOST:=127.0.0.1}"
: "${SIP2_PORT:=3004}"
: "${SIP2_REMOTE_APP:=ils}"

case "${1:-server}" in
  server)
    echo "cds-sip2: starting '${SIP2_SERVER_NAME}' on ${SIP2_BIND_HOST}:${SIP2_PORT} (remote app '${SIP2_REMOTE_APP}')"
    # exec so SIGTERM from the orchestrator reaches the server directly.
    exec invenio selfcheck start "${SIP2_SERVER_NAME}" \
      -h "${SIP2_BIND_HOST}" -p "${SIP2_PORT}" -r "${SIP2_REMOTE_APP}"
    ;;

  healthcheck)
    python - <<'PY'
import os, socket, sys
host = os.environ.get("SIP2_BIND_HOST", "127.0.0.1")
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1"
port = int(os.environ.get("SIP2_PORT", "3004"))
try:
    socket.create_connection((host, port), timeout=2).close()
except OSError as exc:
    print(f"cds-sip2: not accepting on {host}:{port}: {exc}", file=sys.stderr)
    sys.exit(1)
PY
    ;;

  *)
    exec "$@"
    ;;
esac
