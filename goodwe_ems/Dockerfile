ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}

# goodwe is pure-Python; no apt packages needed (options are read in Python).
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN pip install --no-cache-dir goodwe==0.4.10

WORKDIR /app
COPY monitor.py ha_bridge.py price.py ./
COPY static ./static

# Options are read from /data/options.json inside monitor.py. init: false in config.yaml.
CMD ["python3", "/app/monitor.py"]
