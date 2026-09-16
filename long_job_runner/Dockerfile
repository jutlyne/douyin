FROM python:3.11-alpine

RUN apk upgrade --no-cache

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RUN_REGION=asia-southeast1 \
    JOB_NAME=long-coordinator

WORKDIR /app
COPY long_job_runner/requirements.txt /app/requirements.txt
RUN pip uninstall -y setuptools wheel \
    && pip install --upgrade pip \
    && pip install --only-binary=:all: -r /app/requirements.txt \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.11/ensurepip
COPY long_job_runner /app/long_job_runner

CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 \
    --timeout ${GUNICORN_TIMEOUT:-3600} long_job_runner.app:app
