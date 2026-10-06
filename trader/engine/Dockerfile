FROM python:3.12.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# Schema migrations (the `migrate` compose service runs `python -m app.db.migrate`).
COPY alembic.ini ./
COPY alembic ./alembic
# One-off maintenance and data-repair scripts (python -m scripts.<name>).
COPY scripts ./scripts

RUN useradd -m -u 10001 trader && mkdir -p /data && chown -R trader:trader /data /srv
USER trader

ENV PYTHONPATH=/srv
EXPOSE 8000

CMD ["python", "-m", "app.workers.trader"]
