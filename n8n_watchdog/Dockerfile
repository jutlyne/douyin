FROM gcr.io/google.com/cloudsdktool/google-cloud-cli:slim

WORKDIR /app
COPY watchdog.py /app/watchdog.py

ENV CLOUDSDK_CORE_DISABLE_PROMPTS=1

ENTRYPOINT ["python3", "/app/watchdog.py"]
