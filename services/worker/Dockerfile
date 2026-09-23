FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY packages/contracts /app/packages/contracts
COPY packages/sdk /app/packages/sdk
COPY plugins/builtin /app/plugins/builtin
COPY services/worker /app/worker
RUN pip install --no-cache-dir \
    /app/packages/contracts \
    /app/packages/sdk \
    /app/plugins/builtin \
    /app/worker

CMD ["allfeeds-worker"]
