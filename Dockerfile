FROM python:3.13-slim

# Debian's Chromium and its matching chromedriver
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    HEARTBEAT_FILE=/tmp/notflix-heartbeat

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY notflix-parser.py .

RUN useradd --create-home notflix
USER notflix

# Unhealthy when the last successful mail check is older than 5 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=2m \
    CMD python -c "import os,sys,time; sys.exit(time.time() - os.path.getmtime(os.environ['HEARTBEAT_FILE']) > 300)"

CMD ["python", "notflix-parser.py"]
