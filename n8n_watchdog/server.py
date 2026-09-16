import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required env: {name}")
    return value


def fetch(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "n8n-recovery-service/1.0"},
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


def restart_n8n() -> str:
    project_id = env("PROJECT_ID")
    region = env("REGION")
    service = env("N8N_SERVICE", "n8n-selfhost")
    marker_name = env("RESTART_MARKER_ENV", "N8N_MANUAL_RESTART_TS")
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
    print("Restarting n8n Cloud Run service via manual recovery:", timestamp, flush=True)
    subprocess.run(cmd, check=True)
    return timestamp


class Handler(BaseHTTPRequestHandler):
    server_version = "n8n-recovery/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def parsed(self):
        return urllib.parse.urlparse(self.path)

    def require_token(self, parsed) -> bool:
        expected = env("RECOVERY_TOKEN")
        params = urllib.parse.parse_qs(parsed.query)
        supplied = params.get("token", [""])[0]
        header = self.headers.get("X-Recovery-Token", "")
        if supplied == expected or header == expected:
            return True
        self.send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def route(self) -> None:
        parsed = self.parsed()
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self.send_json(200, {
                "ok": True,
                "service": "n8n-recovery",
                "endpoints": ["/status?token=...", "/restart?token=..."],
            })
            return

        if path == "/health":
            self.send_json(200, {"ok": True})
            return

        if path not in {"/status", "/restart"}:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return

        if not self.require_token(parsed):
            return

        push_url = env("N8N_PUSH_URL")
        timeout = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
        status, body = fetch(push_url, timeout)
        body_preview = body.replace("\n", " ")[:500]

        if path == "/status":
            self.send_json(200, {
                "ok": True,
                "n8n_push_status": status,
                "n8n_push_body": body_preview,
                "healthy": status == 401,
            })
            return

        try:
            marker = restart_n8n()
        except Exception as exc:
            self.send_json(500, {
                "ok": False,
                "error": "restart_failed",
                "detail": repr(exc),
                "n8n_push_status_before_restart": status,
                "n8n_push_body_before_restart": body_preview,
            })
            return

        self.send_json(200, {
            "ok": True,
            "status": "restart_requested",
            "restart_marker": marker,
            "n8n_push_status_before_restart": status,
            "n8n_push_body_before_restart": body_preview,
        })


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"n8n recovery service listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()