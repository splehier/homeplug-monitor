# Stage 1: compile plctool from open-plc-utils.
# open-plc-utils is Copyright (c) 2013 Qualcomm Atheros, Inc., under a
# BSD-style three-clause licence. It is fetched and built here, not vendored;
# images built from this file contain plctool and carry its licence terms.
# Only plctool is built — the full suite takes far longer and nothing else is used.
FROM debian:bookworm-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
ARG OPEN_PLC_UTILS_REF=master
RUN git clone --depth 1 --branch "${OPEN_PLC_UTILS_REF}" \
        https://github.com/qca/open-plc-utils.git . \
    && make -C plc plctool \
    && ./plc/plctool -!

# Stage 2: runtime. plctool links against libc only, so the slim image is enough.
FROM python:3.12-slim-bookworm

COPY --from=build /src/plc/plctool /usr/local/bin/plctool

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plc_monitor.py plc.py index.html ./
# The terminal watcher ships too, for `docker exec -it plc-monitor plcwatch`.
COPY plcwatch.py /usr/local/bin/plcwatch
RUN chmod +x /usr/local/bin/plcwatch

# The container needs CAP_NET_RAW and host networking to see the adapter;
# both are granted in docker-compose.yml, not here.
# plcwatch lives on PATH but imports the shared parser from /app.
ENV PYTHONPATH=/app \
    PLC_PLCTOOL=/usr/local/bin/plctool \
    PLC_HTTP_HOST=0.0.0.0 \
    PLC_HTTP_PORT=8087 \
    PYTHONUNBUFFERED=1

EXPOSE 8087

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
url='http://127.0.0.1:'+os.environ.get('PLC_HTTP_PORT','8087')+'/healthz'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status==200 else 1)"

CMD ["python", "/app/plc_monitor.py"]
