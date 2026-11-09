# Prefer the root Dockerfile (API + worker). Kept for older compose paths.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY packages/core ./packages/core
COPY services/api ./services/api
COPY services/worker ./services/worker
RUN pip install --no-cache-dir -e ".[postgres]"
ENV PYTHONPATH=/app/packages/core:/app/services/api:/app/services/worker
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
