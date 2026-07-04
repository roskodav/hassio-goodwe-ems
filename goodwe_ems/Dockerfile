ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

# HA base images are Alpine and ship no Python — install it, then goodwe (pure-Python).
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages goodwe==0.4.10

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY monitor.py ha_bridge.py price.py logic.py ./
COPY static ./static

# Bypass the base image's s6 init and run our server directly (init: false in config.yaml).
# Options are read from /data/options.json inside monitor.py.
ENTRYPOINT []
CMD ["python3", "/app/monitor.py"]
