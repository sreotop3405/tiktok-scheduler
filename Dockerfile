# syntax=docker/dockerfile:1.7
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

WORKDIR /app

COPY pyproject.toml README.md ./
COPY tiktok_scheduler ./tiktok_scheduler

RUN pip install --upgrade pip && pip install -e .

# Browsers come pre-installed in the Playwright image, but make sure the
# version we depend on is present.
RUN python -m playwright install chromium --with-deps || true

VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "tiktok_scheduler", "serve"]
