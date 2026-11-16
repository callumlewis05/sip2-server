# syntax=docker/dockerfile:1
#
# CDS SIP2 server. No stunnel in here: TLS is terminated by a separate stunnel
# container in the same pod, which forwards plaintext SIP2 to 127.0.0.1:3004.
# Pure-Python dependencies only, so no build stage is needed.

FROM python:3.14-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY cds_sip2 ./cds_sip2
RUN pip install --no-deps . && rm -rf /src

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

USER 1001
WORKDIR /home/sip2

# SIP2 (3004) is loopback-only inside the pod; only the health port is reachable.
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["server"]
