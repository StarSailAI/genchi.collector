FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ALLFEEDS_ALEMBIC_ROOT=/app/controller
WORKDIR /app

COPY packages/contracts /app/packages/contracts
COPY services/controller /app/controller
RUN pip install --no-cache-dir /app/packages/contracts /app/controller

WORKDIR /app/controller
CMD ["allfeeds-control", "serve", "--host", "0.0.0.0", "--port", "8060"]
