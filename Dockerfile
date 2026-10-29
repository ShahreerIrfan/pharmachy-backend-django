# Multi-stage Dockerfile for Bangladesh Pharmacy SaaS Cloud Authority API
# Optimized for Dokploy / Linux Server Docker Deployment
# Architecture Decision: ADR-0004, ADR-0021 & ADR-0031

FROM python:3.13-slim as builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Final Production Stage
FROM python:3.13-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . /app

ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE="config.settings"

EXPOSE 8000

# Healthcheck for Dokploy container monitoring
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--access-logfile", "-", "config.wsgi:application"]
