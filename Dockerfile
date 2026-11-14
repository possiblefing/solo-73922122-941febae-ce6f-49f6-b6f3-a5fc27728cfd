# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENERGY_DB_PATH=/data/energy.sqlite3 \
    ENERGY_BUSY_TIMEOUT_MS=30000

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data \
    && useradd --system --uid 10001 --home-dir /srv energy \
    && chown -R energy:energy /data /srv
USER energy

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=12 \
    CMD python -c "import urllib.request; \
urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "app.run"]
