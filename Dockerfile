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

RUN mkdir -p /data/parquet

EXPOSE 8000

# Default: API. Cron services override the command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
