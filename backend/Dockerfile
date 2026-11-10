FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DEBUG=false \
    MUSIC_OUTPUT_DIR=/music \
    MUSIC_SEERR_CONFIG=/app/data/config.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 musicseerr \
    && useradd --uid 10001 --gid musicseerr --home-dir /app --no-create-home musicseerr

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=musicseerr:musicseerr app ./app

RUN mkdir -p /music /app/data \
    && chown -R musicseerr:musicseerr /music /app/data

USER musicseerr:musicseerr
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
