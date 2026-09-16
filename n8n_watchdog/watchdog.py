import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required env: {name}")
    return value


def fetch(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "n8n-cloud-run-watchdog/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        return exc.code, body
    except Exception as exc:
        return 0, repr(exc)


def should_restart(status: int, body: str) -> bool:
    restart_statuses = {
        int(part.strip())
        for part in os.environ.get("RESTART_ON_STATUS", "503").split(",")
        if part.strip()
    }
    body_needles = [
        part.strip()
        for part in os.environ.get("RESTART_ON_BODY", "Database is not ready").split("|")
        if part.strip()
    ]
    if status not in restart_statuses:
        return False
    return any(needle in body for needle in body_needles)


def restart_n8n() -> None:
    project_id = env("PROJECT_ID")
    region = env("REGION")
    service = env("N8N_SERVICE", "n8n-selfhost")
    marker_name = env("RESTART_MARKER_ENV", "N8N_WATCHDOG_RESTART_TS")
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    cmd = [
        "gcloud",
        "run",
        "services",
        "update",
        service,
        "--project",
        project_id,
        "--region",
        region,
        "--update-env-vars",
        f"{marker_name}={timestamp}",
        "--quiet",
    ]
    print("Restarting n8n Cloud Run service via rollout marker:", timestamp, flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    url = env("N8N_PUSH_URL")
    timeout = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
    status, body = fetch(url, timeout)
    body_preview = body.replace("\n", " ")[:500]
    print(f"n8n push check status={status} body={body_preview!r}", flush=True)

    if should_restart(status, body):
        if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
            print("DRY_RUN enabled; restart skipped.", flush=True)
            return 0
        restart_n8n()
        return 0

    print("No restart needed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"watchdog failed: {exc}", file=sys.stderr, flush=True)
        raise
