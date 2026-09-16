FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DESUB_DETECT_FPS=8 \
    DESUB_MODEL=lama \
    DESUB_STROKE_MASK_ENABLED=true \
    DESUB_STROKE_WHITE_THRESHOLD=200 \
    DESUB_STROKE_BLACK_THRESHOLD=60 \
    DESUB_STROKE_REQUIRE_DARK_TOUCH=false \
    DESUB_STROKE_TOUCH_RADIUS_PX=4 \
    DESUB_STROKE_DILATE_PX=3 \
    DESUB_STROKE_FALLBACK_MIN_RATIO=0.15 \
    DESUB_STROKE_FEATHER_PX=2 \
    DESUB_LAMA_TEMPORAL_BLEND=0.5 \
    DESUB_MASK_DILATE_PX=10 \
    DESUB_BAND_TOP_RATIO=0.66 \
    DESUB_MAX_SPAN_BAND_AREA_RATIO=0.20 \
    DESUB_COMPARE_PADDLE=false \
    DESUB_WATERMARK_MASKS_ENABLED=true \
    DESUB_ALLOW_RUNTIME_MODEL_DOWNLOAD=false \
    EASYOCR_MODEL_DIR=/models/easyocr

RUN apt-get update \
    && apt-get -y dist-upgrade \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

# All Python deps are individually pinned and intentionally do not use VSR's
# requirements.txt. ProPainter and paddle-serving are not installed.
# Cloud Run L4 ships NVIDIA driver CUDA 12.2 — default PyPI torch wheels are
# built against a newer CUDA and silently fall back to CPU. Use cu126 wheels
# (CUDA 12.x minor-version compatibility covers the 12.2 driver).
RUN python -m pip install --upgrade pip==26.1.2 setuptools==80.9.0 \
    && python -m pip install --only-binary=:all: \
        --index-url https://download.pytorch.org/whl/cu126 \
        torch==2.12.1+cu126 \
        torchvision==0.27.1+cu126 \
    && python -m pip install --only-binary=:all: \
        easyocr==1.7.2 \
        paddlepaddle==3.3.1 \
        paddleocr==3.7.0 \
        opencv-python-headless==5.0.0.93 \
        google-genai==2.11.0 \
        google-cloud-storage==3.12.0 \
        requests==2.34.2 \
        yt-dlp==2026.7.4 \
        Pillow==12.3.0 \
        tqdm==4.68.3 \
        scikit-image==0.26.0 \
        einops==0.8.2 \
        av==18.0.0 \
        scipy==1.18.0 \
        matplotlib==3.11.0 \
        filesplit==4.1.0

WORKDIR /app
COPY experiments/desub/prototype.py /app/experiments/desub/prototype.py

ARG VSR_REF=1.4.0
RUN git clone --depth 1 --branch "${VSR_REF}" https://github.com/YaoFANGUK/video-subtitle-remover.git /opt/video-subtitle-remover

WORKDIR /opt/video-subtitle-remover
RUN rm -rf backend/inpaint/propainter* backend/models/propainter \
    && python /app/experiments/desub/prototype.py --patch-vsr /opt/video-subtitle-remover

# Bake OCR and inpainting weights into the image. These are downloaded from the
# official package/model sources during Cloud Build, not at Cloud Run runtime.
RUN python /app/experiments/desub/prototype.py --bake-models /opt/video-subtitle-remover

WORKDIR /app
COPY douyin_api /app/douyin_api
COPY container_short/steps/gcsio.py /app/container_short/steps/gcsio.py

ENTRYPOINT ["python", "/app/experiments/desub/prototype.py"]
