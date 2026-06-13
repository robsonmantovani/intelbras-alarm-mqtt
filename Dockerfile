FROM python:3.11-slim

LABEL maintainer="mantovani"
LABEL description="Intelbras Alarm MQTT Bridge - ISECNet to MQTT with Home Assistant auto-discovery"

# Install dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY app.py /app/
COPY lib/ /app/lib/

WORKDIR /app

# Default config path (override with CONFIG_PATH env var)
ENV CONFIG_PATH=/config/config.yml

# Health check: verify process is running
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python.*app.py" || exit 1

ENTRYPOINT ["python", "-u", "app.py"]
