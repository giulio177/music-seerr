FROM python:3.12-slim

ARG APP_VERSION=dev

LABEL org.opencontainers.image.source="https://github.com/giulio177/sonalis"

ENV PYTHONDONTWRITEBYTECODE=1 \
    SONALIS_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DEBUG=false \
    MUSIC_OUTPUT_DIR=/music \
    SONALIS_CONFIG=/app/data/config.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sonalis \
    && useradd --uid 10001 --gid sonalis --home-dir /app --no-create-home sonalis

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=sonalis:sonalis app ./app

RUN mkdir -p /music /app/data \
    && chown -R sonalis:sonalis /music /app/data

USER sonalis:sonalis
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
