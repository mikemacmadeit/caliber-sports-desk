# Caliber API + worker image (Render / Fly / Railway / Docker Compose)
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY packages/core ./packages/core
COPY services/api ./services/api
COPY services/worker ./services/worker

# Postgres driver required in cloud; SQLite still works locally.
RUN pip install --no-cache-dir -e ".[postgres]"

ENV PYTHONPATH=/app/packages/core:/app/services/api:/app/services/worker
ENV PARQUET_DIR=/data/parquet
ENV APP_ENV=prod

# Model weights + research/ATS board caches so cloud serves like local.
# Fresh DB rows still come from Postgres; these files are the trained artifacts.
COPY deploy/runtime-data/parquet /data/parquet
COPY deploy/runtime-data/research /data/research
COPY deploy/runtime-data/cache /data/cache
RUN mkdir -p /data/parquet /data/research /data/cache

EXPOSE 8000

# Shell form so ${PORT} works on Render (they inject PORT; local defaults to 8000).
# Cron / one-off jobs override the command in the host config.
CMD sh -c 'uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}'
