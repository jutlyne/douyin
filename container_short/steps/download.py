"""B1 — tải video Douyin về file local, tái dùng douyin_api.douyin_downloader."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass

# Cho phép import douyin_api dù chạy local hay trong container (xem Dockerfile copy).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from douyin_api.douyin_downloader import DouyinVideo, open_stream, resolve  # noqa: E402


@dataclass(frozen=True)
class _BrowserProfile:
    path: str
    user_agent: str


def _chromium_binary() -> str:
    for candidate in ("chromium-browser", "chromium", "google-chrome"):
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise RuntimeError(
        "Chromium is unavailable; cannot mint a fresh anonymous Douyin session"
    )


def _chromium_user_agent(executable: str) -> str:
    try:
        version_text = subprocess.check_output(
            [executable, "--version"],
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        version_text = ""
    match = re.search(r"(\d+)(?:\.\d+){2,3}", version_text)
    major = match.group(1) if match else "140"
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


@contextlib.contextmanager
def _fresh_douyin_browser_profile(url: str) -> Iterator[_BrowserProfile]:
    """Let Douyin mint signed anonymous cookies in a real browser.

    The temporary profile stays on the same Cloud Run task/IP as yt-dlp and is
    destroyed immediately after extraction. Cookie values are never logged.
    """
    executable = _chromium_binary()
    profile_path = tempfile.mkdtemp(prefix="douyin_chromium_")
    user_agent = _chromium_user_agent(executable)
    wait_ms = max(5_000, int(os.environ.get("DOUYIN_BROWSER_WAIT_MS", "15000")))
    command = [
        executable,
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--lang=zh-CN",
        "--window-size=1365,768",
        f"--user-data-dir={profile_path}",
        f"--user-agent={user_agent}",
        f"--virtual-time-budget={wait_ms}",
        "--dump-dom",
        url,
    ]
    try:
        print("[download] minting a fresh anonymous Douyin browser session", flush=True)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(
                    20,
                    int(os.environ.get("DOUYIN_BROWSER_TIMEOUT_SECONDS", "30")),
                ),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Modern web pages keep background connections alive indefinitely.
            # subprocess.run has already stopped Chromium; its cookie database
            # remains in the temporary profile and is ready for yt-dlp.
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            completed = subprocess.CompletedProcess(command, 124, "", stderr)
            print(
                "[download] browser wait elapsed; continuing with minted cookies",
                flush=True,
            )
        cookie_db_found = any(
            os.path.isfile(os.path.join(root, filename))
            for root, _dirs, files in os.walk(profile_path)
            for filename in files
            if filename == "Cookies"
        )
        if not cookie_db_found:
            detail = (completed.stderr or "").strip().splitlines()
            suffix = f": {detail[-1][:240]}" if detail else ""
            raise RuntimeError(f"Chromium did not create a cookie store{suffix}")
        yield _BrowserProfile(path=profile_path, user_agent=user_agent)
    finally:
        shutil.rmtree(profile_path, ignore_errors=True)


def _metadata_to_video(metadata: dict) -> DouyinVideo:
    return DouyinVideo(
        aweme_id=str(metadata.get("id") or "").strip(),
        desc=str(metadata.get("description") or metadata.get("title") or "").strip(),
        author=str(metadata.get("uploader") or "").strip(),
        duration_ms=int(float(metadata.get("duration") or 0) * 1000),
        cover=str(metadata.get("thumbnail") or ""),
        play_url=str(metadata.get("url") or ""),
    )


def _ytdlp_extract(
    url: str,
    *,
    dst_path: str | None,
    cookie: str | None,
    browser: _BrowserProfile | None = None,
) -> tuple[dict, str | None]:
    import yt_dlp  # type: ignore

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "nopart": True,
        "http_headers": ({"Cookie": cookie} if cookie else {}),
    }
    if dst_path:
        options.update({
            "format": "bestvideo*+bestaudio/best",
            "outtmpl": dst_path,
            "merge_output_format": "mp4",
            "overwrites": True,
        })
    else:
        options["skip_download"] = True
    if browser:
        options["cookiesfrombrowser"] = ("chromium", browser.path, None, None)
        options["http_headers"] = {
            "User-Agent": browser.user_agent,
            "Referer": "https://www.douyin.com/",
        }
    with yt_dlp.YoutubeDL(options) as ydl:
        metadata = ydl.extract_info(url, download=bool(dst_path))
    return metadata, dst_path


def _download_with_ytdlp(
    url_or_text: str,
    dst_path: str,
    *,
    cookie: str | None,
) -> DouyinVideo:
    """Latest-patched yt-dlp fallback; never invokes shell commands."""
    url = str(url_or_text).strip()
    first_error: Exception | None = None
    metadata: dict
    if cookie:
        try:
            metadata, _ = _ytdlp_extract(
                url,
                dst_path=dst_path,
                cookie=cookie,
            )
        except Exception as exc:  # noqa: BLE001 - refresh a dead cookie
            first_error = exc
            print("[download] supplied Douyin cookie failed; refreshing", flush=True)
        else:
            first_error = None
    if not cookie or first_error is not None:
        with _fresh_douyin_browser_profile(url) as browser:
            metadata, _ = _ytdlp_extract(
                url,
                dst_path=dst_path,
                cookie=None,
                browser=browser,
            )
    if not os.path.exists(dst_path) or os.path.getsize(dst_path) <= 0:
        prepared = str(metadata.get("requested_downloads", [{}])[0].get("filepath") or "")
        if prepared and os.path.exists(prepared):
            os.replace(prepared, dst_path)
    if not os.path.exists(dst_path) or os.path.getsize(dst_path) <= 0:
        raise RuntimeError("yt-dlp completed without an output video")
    return _metadata_to_video(metadata)


def probe_douyin(url_or_text: str, cookie: str | None = None) -> DouyinVideo:
    """Resolve metadata while exercising the same fresh-cookie fallback."""
    try:
        return resolve(url_or_text, cookie=cookie)
    except Exception as primary_error:  # noqa: BLE001
        print(
            "[download] built-in Douyin probe failed; trying browser session: "
            f"{primary_error}",
            flush=True,
        )
    with _fresh_douyin_browser_profile(str(url_or_text).strip()) as browser:
        metadata, _ = _ytdlp_extract(
            str(url_or_text).strip(),
            dst_path=None,
            cookie=None,
            browser=browser,
        )
    return _metadata_to_video(metadata)


def download_douyin(url_or_text: str, dst_path: str, cookie: str | None = None) -> DouyinVideo:
    """Resolve link Douyin và tải mp4 (không logo) về dst_path. Trả metadata."""
    try:
        info: DouyinVideo = resolve(url_or_text, cookie=cookie)
    except Exception as primary_error:  # noqa: BLE001 - patched extractor fallback
        print(
            "[download] built-in Douyin resolver failed; trying yt-dlp: "
            f"{primary_error}",
            flush=True,
        )
        return _download_with_ytdlp(
            url_or_text,
            dst_path,
            cookie=cookie,
        )
    if not info.play_url:
        raise RuntimeError(
            f"Không lấy được link video (aweme_id={info.aweme_id}); "
            "có thể là post ảnh, không xử lý được."
        )

    last_err: Exception | None = None
    attempts = max(1, int(os.environ.get("DOUYIN_DOWNLOAD_ATTEMPTS", "3")))
    candidates = info.play_url_candidates or [info.play_url]
    for attempt in range(attempts):
        for candidate in candidates:
            try:
                resp = open_stream(candidate, cookie=cookie)
                try:
                    with open(dst_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)
                finally:
                    resp.close()
                if os.path.getsize(dst_path) > 0:
                    return info
            except Exception as e:  # noqa: BLE001 - thử ứng viên url/attempt tiếp theo
                last_err = e
                try:
                    if os.path.exists(dst_path):
                        os.remove(dst_path)
                except OSError:
                    pass
                continue
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 5))
    raise RuntimeError(f"Tải video Douyin thất bại: {last_err}")
