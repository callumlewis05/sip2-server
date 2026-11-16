#!/bin/sh
# Entrypoint for the CDS SIP2 server.
#
#   server       start the SIP2 server (default); configuration from CDS_SIP2_*
#   healthcheck  exit 0 if /livez answers 200 (for docker-compose; OpenShift
#                uses httpGet probes on the health port directly)
#   <anything>   exec it as-is (`sh`, `python`, ...)
set -eu

case "${1:-server}" in
  server)
    # exec so SIGTERM from the orchestrator reaches the server directly.
    exec python -m cds_sip2
    ;;

  healthcheck)
    exec python -c "import os, sys, urllib.request
port = os.environ.get('CDS_SIP2_HEALTH_PORT', '8080')
try:
    urllib.request.urlopen(f'http://127.0.0.1:{port}/livez', timeout=2)
except Exception as exc:
    sys.exit(f'cds-sip2: not live: {exc}')"
    ;;

  *)
    exec "$@"
    ;;
esac
