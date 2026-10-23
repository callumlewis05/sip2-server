# syntax=docker/dockerfile:1
#
# CDS SIP2 sidecar. No stunnel in here: TLS is terminated by a separate stunnel
# container in the same pod, which forwards plaintext SIP2 to 127.0.0.1:3004.

# Single source of truth for the base image. Builder and runtime MUST match,
# because the venv built in stage 1 is copied into stage 2.
ARG PYTHON_IMAGE=python:3.14-slim-bookworm

# ---------------------------------------------------------------- builder ---
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# invenio-app pulls in uwsgi, which compiles; keep the toolchain out of the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpcre3-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY cds_sip2 ./cds_sip2
RUN pip install --no-deps .

# ---------------------------------------------------------------- runtime ---
FROM ${PYTHON_IMAGE}

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INVENIO_INSTANCE_PATH=/var/lib/sip2/instance \
    CDS_SIP2_LOG_FILE=/var/log/sip2/sip2.log \
    SIP2_SERVER_NAME=cds \
    SIP2_BIND_HOST=127.0.0.1 \
    SIP2_PORT=3004 \
    SIP2_REMOTE_APP=ils

COPY --from=builder /opt/venv /opt/venv

COPY --chown=1001:0 entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER 1001
WORKDIR /home/sip2

# Deliberately no EXPOSE: the port is loopback-only inside the pod.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["server"]