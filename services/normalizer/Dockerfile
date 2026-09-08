FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GENCHI_ALEMBIC_ROOT=/app/normalizer
WORKDIR /app

COPY services/normalizer /app/normalizer
COPY services/product /app/product
RUN pip install --no-cache-dir /app/normalizer /app/product

WORKDIR /app/normalizer
CMD ["genchi-normalizer", "run"]
