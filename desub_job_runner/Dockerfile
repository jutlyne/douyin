FROM us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-runner:prod

WORKDIR /app
COPY desub_job_runner /app/desub_job_runner
COPY tests/test_desub_job_runner.py /app/test_desub_job_runner.py

CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 \
    --timeout ${GUNICORN_TIMEOUT:-3600} desub_job_runner.app:app
