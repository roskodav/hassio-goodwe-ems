ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

# jq is used by run.sh to read the add-on options; goodwe drives the inverters.
RUN pip install --no-cache-dir goodwe==0.4.10 \
    && apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY monitor.py ha_bridge.py price.py run.sh ./
COPY static ./static
RUN chmod +x run.sh

ENV DATA_DIR=/data
CMD ["./run.sh"]
