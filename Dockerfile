# syntax=docker/dockerfile:1

FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /build/requirements.txt
RUN python -m pip install --upgrade pip \
    && pip wheel --wheel-dir /wheels -r /build/requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    API_HOST=0.0.0.0 \
    API_PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY --from=builder /wheels /wheels

RUN python -m pip install --upgrade pip \
    && pip install --no-index --find-links=/wheels -r /app/requirements.txt \
    && rm -rf /wheels

COPY . /app

RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home /home/appuser appuser \
    && mkdir -p /app/.antihub /app/logs \
    && chown -R appuser:appuser /app /home/appuser

USER appuser

EXPOSE 8000

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "main:app"]
