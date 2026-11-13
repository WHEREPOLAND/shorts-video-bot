FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg/ffprobe нужны для склейки дорожек, превью и сжатия под лимит Telegram.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY bot ./bot

RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /tmp/shorts-video-bot \
    && chown -R botuser:botuser /app /tmp/shorts-video-bot
USER botuser

EXPOSE 8080

CMD ["python", "-m", "bot.main"]
