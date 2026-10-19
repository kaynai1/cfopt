# Multi-stage build: builder installs deps, runtime keeps only what is needed
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim

LABEL org.opencontainers.image.title="cfopt" \
      org.opencontainers.image.description="Cloudflare edge IP optimizer: pick best colo and publish to hosts" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TARGET_HOST="" \
    COLOS="HKG" \
    MODE="hosts" \
    SYSTEM_HOSTS="" \
    PER_NET=2 \
    WORKERS=120 \
    INTERVAL=21600 \
    TOP_N=3 \
    TIMEOUT=4 \
    DNS_LISTEN="0.0.0.0:53" \
    UPSTREAMS="223.5.5.5:53,119.29.29.29:53" \
    HTTP_PORT=8080 \
    DATA_DIR=/data \
    FAIL_FAST=1

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Runs as root because it must write the host /etc/hosts file.
# The container is started with network_mode: host so it shares the host
# network namespace; the only privileged action is that file write.
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 53/udp 53/tcp 8080/tcp

# exec form so SIGTERM reaches the Python process
ENTRYPOINT ["python", "-m", "cfopt.main"]
