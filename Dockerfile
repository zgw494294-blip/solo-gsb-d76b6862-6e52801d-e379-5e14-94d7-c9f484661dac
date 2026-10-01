FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FIRMWARE_DB_PATH=/data/firmware.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite database lives on the persistent named volume mounted at /data.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Single worker: in-process lock plus SQLite transactions make concurrent
# requests equivalent to serial execution.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
