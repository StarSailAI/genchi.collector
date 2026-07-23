FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GENCHI_ALEMBIC_ROOT=/app/normalizer
WORKDIR /app

COPY services/normalizer /app/normalizer
RUN pip install --no-cache-dir /app/normalizer

WORKDIR /app/normalizer
CMD ["genchi-normalizer", "run"]
