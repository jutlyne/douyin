FROM python:3.11-alpine

RUN apk upgrade --no-cache \
    && apk add --no-cache \
        ffmpeg ca-certificates fontconfig font-dejavu font-liberation chromium

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VERTEX_REGION=global \
    GEMINI_MODEL=gemini-2.5-pro \
    LONG_CHUNK_SECONDS=360 \
    LONG_MIN_CHUNK_SECONDS=240 \
    LONG_MAX_CHUNK_SECONDS=480 \
    LONG_ANALYSIS_PADDING_SECONDS=3 \
    BGM_GAIN_DB=-20 \
    DUB_MAX_SPEED=1.35 \
    DUB_HARD_MAX_SPEED=1.45 \
    DUB_END_MICROFIT_MAX_SPEED=1.08 \
    DUB_TAIL_PAD_SECONDS=3.0 \
    LONG_VIDEO_CRF=18 \
    LONG_VIDEO_WIDTH=1920 \
    LONG_VIDEO_HEIGHT=1080 \
    TTS_PROVIDER=capcut \
    TTS_FALLBACK_PROVIDER=google \
    TTS_VOICE=BV074_streaming \
    SPEAKING_RATE=1.0 \
    GOOGLE_TTS_VOICE=vi-VN-Wavenet-B \
    CAPCUT_RESOURCE_ID=7102355709945188865 \
    CAPCUT_POLL_TIMEOUT=300

WORKDIR /app
COPY container_short/requirements.txt /app/requirements.txt
RUN pip uninstall -y setuptools wheel \
    && pip install --upgrade pip \
    && pip install --only-binary=:all: -r /app/requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.11/ensurepip
COPY douyin_api /app/douyin_api
COPY container_short /app/container_short
COPY container_long /app/container_long

ENTRYPOINT ["python", "-m", "container_long.worker_job"]
