"""Core Douyin resolver.

Phương pháp: lấy aweme_id từ link chia sẻ, rồi gọi trang chia sẻ
`iesdouyin.com/share/video/{id}` — trang này nhúng sẵn JSON trong
`window._ROUTER_DATA`, không cần ký a_bogus/X-Bogus.

Lưu ý: Douyin thỉnh thoảng đổi cấu trúc JSON, khi đó cần chỉnh
_resolve_aweme_id() / _parse_router_data().
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import requests

# UA mobile giúp trang share trả về JSON đầy đủ hơn.
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}

# Bắt mọi URL douyin trong đoạn text "copy" mà app Douyin sinh ra.
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
# aweme_id: chuỗi số dài.
_ID_RE = re.compile(r"(?:video|note|modal_id=)/?(\d{15,21})")
_ROUTER_RE = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL
)


class DouyinError(Exception):
    """Lỗi nghiệp vụ khi resolve link Douyin."""


@dataclass
class DouyinVideo:
    aweme_id: str
    desc: str = ""
    author: str = ""
    duration_ms: int = 0
    cover: str = ""
    # URL phát không logo (đã chọn bản tốt nhất).
    play_url: str = ""
    # Toàn bộ ứng viên url_list (fallback nếu url đầu hỏng).
    play_url_candidates: list[str] = field(default_factory=list)
    music_url: str = ""
    # Với "note" dạng ảnh: danh sách ảnh.
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _new_session(cookie: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def extract_url(text: str) -> str:
    """Tách URL douyin đầu tiên từ đoạn text người dùng dán."""
    m = _URL_RE.search(text or "")
    if not m:
        raise DouyinError("Không tìm thấy URL Douyin trong nội dung được cung cấp.")
    return m.group(0).rstrip(".,)；;")


def _resolve_aweme_id(url: str, session: requests.Session) -> str:
    """Theo redirect của link rút gọn (v.douyin.com) để lấy aweme_id."""
    m = _ID_RE.search(url)
    if m:
        return m.group(1)
    # Link rút gọn: cần follow redirect.
    try:
        resp = session.get(url, allow_redirects=True, timeout=10)
    except requests.RequestException as e:
        raise DouyinError(f"Không mở được link: {e}") from e
    final = resp.url
    m = _ID_RE.search(final)
    if m:
        return m.group(1)
    # Đôi khi id nằm trong body.
    m = _ID_RE.search(resp.text or "")
    if m:
        return m.group(1)
    raise DouyinError(f"Không trích xuất được aweme_id từ: {final}")


def _parse_router_data(html: str, aweme_id: str) -> dict[str, Any]:
    m = _ROUTER_RE.search(html)
    if not m:
        raise DouyinError(
            "Không tìm thấy _ROUTER_DATA — Douyin có thể đã đổi cấu trúc "
            "hoặc yêu cầu xác thực (thử thêm cookie)."
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise DouyinError(f"JSON _ROUTER_DATA không hợp lệ: {e}") from e

    loader = data.get("loaderData", {})
    # Key page khác nhau giữa video/note; tìm key có 'videoInfoRes'.
    for _key, val in loader.items():
        if isinstance(val, dict) and "videoInfoRes" in val:
            items = val["videoInfoRes"].get("item_list") or []
            if items:
                return items[0]
            # Một số phản hồi để item trong 'filter_list' khi bị chặn.
            filt = val["videoInfoRes"].get("filter_list") or []
            if filt:
                raise DouyinError(
                    f"Douyin chặn nội dung: {filt[0].get('detail_msg', 'unknown')}"
                )
    raise DouyinError(f"Không có item_list cho aweme_id={aweme_id}.")


def _parse_item_payload(payload: Any, aweme_id: str) -> dict[str, Any]:
    """Extract one aweme item from known public JSON response shapes."""
    if not isinstance(payload, dict):
        raise DouyinError("Phản hồi metadata Douyin không phải JSON object.")
    direct = payload.get("aweme_detail")
    if isinstance(direct, dict) and direct:
        return direct
    for key in ("item_list", "aweme_list"):
        items = payload.get(key) or []
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    data = payload.get("data")
    if isinstance(data, dict):
        direct = data.get("aweme_detail")
        if isinstance(direct, dict) and direct:
            return direct
        for key in ("item_list", "aweme_list"):
            items = data.get(key) or []
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0]
    raise DouyinError(f"JSON không có item cho aweme_id={aweme_id}.")


def _resolve_item_with_fallbacks(
    aweme_id: str,
    *,
    cookie: Optional[str],
) -> dict[str, Any]:
    """Resolve metadata across share-page and public JSON variants.

    Douyin intermittently returns an empty ``item_list`` to one user-agent or
    endpoint. Each attempt creates a fresh session and uses a cache-busting
    query so a transient empty response does not poison a long Cloud Run job.
    """
    errors: list[str] = []
    attempts = 3
    for attempt in range(attempts):
        for user_agent in (_MOBILE_UA, _DESKTOP_UA):
            session = _new_session(cookie)
            session.headers["User-Agent"] = user_agent
            share_urls = (
                f"https://www.iesdouyin.com/share/video/{aweme_id}/"
                f"?verifyFp=&fp=&msToken=&_={int(time.time() * 1000)}",
                f"https://www.douyin.com/video/{aweme_id}",
            )
            for share_url in share_urls:
                try:
                    response = session.get(share_url, timeout=20)
                    response.raise_for_status()
                    return _parse_router_data(response.text, aweme_id)
                except Exception as exc:  # noqa: BLE001 - try next variant
                    errors.append(f"share:{type(exc).__name__}:{exc}")

            json_urls = (
                (
                    "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/",
                    {"item_ids": aweme_id},
                ),
                (
                    "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                    {
                        "aweme_id": aweme_id,
                        "aid": "6383",
                        "device_platform": "webapp",
                    },
                ),
            )
            for api_url, params in json_urls:
                try:
                    response = session.get(api_url, params=params, timeout=20)
                    response.raise_for_status()
                    return _parse_item_payload(response.json(), aweme_id)
                except Exception as exc:  # noqa: BLE001 - try next variant
                    errors.append(f"json:{type(exc).__name__}:{exc}")
        if attempt + 1 < attempts:
            time.sleep(1.5 * (attempt + 1))
    detail = errors[-1] if errors else "unknown"
    raise DouyinError(
        f"Không lấy được metadata aweme_id={aweme_id} sau "
        f"{attempts} lần thử: {detail}"
    )


def _best_play_url(play_addr: dict[str, Any]) -> tuple[str, list[str]]:
    urls = [u for u in (play_addr.get("url_list") or []) if u]
    # Ưu tiên url không có 'playwm' (bản có logo).
    no_wm = [u for u in urls if "playwm" not in u]
    cleaned = no_wm or [u.replace("playwm", "play") for u in urls]
    return (cleaned[0] if cleaned else ""), cleaned


def resolve(url_or_text: str, cookie: Optional[str] = None) -> DouyinVideo:
    """Resolve một link/đoạn text Douyin thành DouyinVideo."""
    session = _new_session(cookie)
    url = extract_url(url_or_text)
    aweme_id = _resolve_aweme_id(url, session)

    item = _resolve_item_with_fallbacks(aweme_id, cookie=cookie)

    video = item.get("video") or {}
    play_url, candidates = _best_play_url(video.get("play_addr") or {})

    cover_list = (video.get("cover") or {}).get("url_list") or []
    music = (item.get("music") or {}).get("play_url") or {}
    music_url = (music.get("url_list") or [""])[0] if isinstance(music, dict) else ""

    # Note dạng ảnh.
    images: list[str] = []
    for img in item.get("images") or []:
        url_list = (img or {}).get("url_list") or []
        if url_list:
            images.append(url_list[-1])

    return DouyinVideo(
        aweme_id=aweme_id,
        desc=(item.get("desc") or "").strip(),
        author=((item.get("author") or {}).get("nickname") or "").strip(),
        duration_ms=int(video.get("duration") or 0),
        cover=cover_list[0] if cover_list else "",
        play_url=play_url,
        play_url_candidates=candidates,
        music_url=music_url,
        images=images,
    )


def open_stream(play_url: str, cookie: Optional[str] = None) -> requests.Response:
    """Mở stream tới file video. Caller phải close() response sau khi dùng."""
    if not play_url:
        raise DouyinError("play_url rỗng.")
    session = _new_session(cookie)
    try:
        resp = session.get(play_url, stream=True, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise DouyinError(f"Tải video thất bại: {e}") from e
    return resp


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Dùng: python douyin_downloader.py <link douyin>")
        raise SystemExit(1)
    info = resolve(sys.argv[1])
    print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
