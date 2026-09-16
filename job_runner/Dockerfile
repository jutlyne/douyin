FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY job_runner/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY job_runner/app.py /app/app.py
COPY job_runner/douyin_channel.py /app/douyin_channel.py

CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 1000 app:app
