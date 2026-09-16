"""Liệt kê video mới nhất của một KÊNH (creator) Douyin.

Cùng kỹ thuật với douyin_api/douyin_downloader.py: trang share của Douyin
nhúng sẵn JSON trong ``window._ROUTER_DATA`` nên không cần ký a_bogus/X-Bogus.
Ở đây ta nhắm tới trang share của *user* thay vì *video*:

    https://www.iesdouyin.com/share/user/{sec_uid}

JSON này chứa danh sách bài đăng (post list). Vì Douyin hay đổi cấu trúc,
ta KHÔNG bám cứng key mà duyệt đệ quy để gom mọi item có ``aweme_id``.

Lưu ý: yt-dlp (2026.06) không có extractor douyin:user nên không liệt kê được
kênh — đó là lý do dùng hướng iesdouyin nhẹ này.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Optional

import requests

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
)

_HEADERS = {
    "User-Agent": _MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
# sec_uid của Douyin luôn bắt đầu bằng "MS4w".
_SEC_UID_RE = re.compile(r"(MS4w[\w-]{10,})")
_ROUTER_RE = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL
)
# aweme_id: chuỗi số dài 15-21 chữ số.
_AWEME_ID_RE = re.compile(r"^\d{15,21}$")


class DouyinChannelError(Exception):
    """Lỗi nghiệp vụ khi liệt kê video của kênh Douyin."""


@dataclass
class ChannelVideo:
    aweme_id: str
    desc: str = ""
    create_time: int = 0
    duration_ms: int = 0
    share_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _new_session(cookie: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def extract_url(text: str) -> str:
    """Tách URL đầu tiên trong đoạn text 'copy link' của Douyin."""
    m = _URL_RE.search(text or "")
    if not m:
        raise DouyinChannelError("Không tìm thấy URL Douyin trong nội dung.")
    return m.group(0).rstrip(".,)；;")


def _resolve_sec_uid(channel_url: str, session: requests.Session) -> str:
    """Lấy sec_uid từ link trang cá nhân (hoặc link rút gọn v.douyin.com)."""
    m = _SEC_UID_RE.search(channel_url)
    if m:
        return m.group(1)
    # Link rút gọn → follow redirect tới trang user thật.
    try:
        resp = session.get(channel_url, allow_redirects=True, timeout=15)
    except requests.RequestException as e:
        raise DouyinChannelError(f"Không mở được link kênh: {e}") from e
    m = _SEC_UID_RE.search(resp.url) or _SEC_UID_RE.search(resp.text or "")
    if m:
        return m.group(1)
    raise DouyinChannelError(
        f"Không trích xuất được sec_uid từ: {resp.url}. "
        "Hãy đưa link trang cá nhân Douyin (douyin.com/user/MS4w...)."
    )


def _iter_aweme_items(node: Any) -> Iterable[dict[str, Any]]:
    """Duyệt đệ quy cây JSON, trả mọi dict có 'aweme_id' hợp lệ."""
    if isinstance(node, dict):
        aid = node.get("aweme_id") or node.get("awemeId")
        if isinstance(aid, (str, int)) and _AWEME_ID_RE.match(str(aid)):
            yield node
        for value in node.values():
            yield from _iter_aweme_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_aweme_items(value)


def _parse_router_users_posts(html: str) -> list[dict[str, Any]]:
    m = _ROUTER_RE.search(html)
    if not m:
        raise DouyinChannelError(
            "Không tìm thấy _ROUTER_DATA — Douyin có thể đã đổi cấu trúc "
            "hoặc cần cookie đăng nhập."
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise DouyinChannelError(f"_ROUTER_DATA không phải JSON hợp lệ: {e}") from e
    items: dict[str, dict[str, Any]] = {}
    for item in _iter_aweme_items(data):
        aid = str(item.get("aweme_id") or item.get("awemeId"))
        # Giữ bản đầy đủ nhất (có 'desc'/'video') nếu trùng aweme_id.
        if aid not in items or (item.get("desc") and not items[aid].get("desc")):
            items[aid] = item
    return list(items.values())


def _to_video(item: dict[str, Any]) -> ChannelVideo:
    aid = str(item.get("aweme_id") or item.get("awemeId"))
    video = item.get("video") or {}
    return ChannelVideo(
        aweme_id=aid,
        desc=(item.get("desc") or "").strip(),
        create_time=int(item.get("create_time") or item.get("createTime") or 0),
        duration_ms=int(video.get("duration") or 0),
        share_url=f"https://www.douyin.com/video/{aid}",
    )


def list_user_videos(
    channel_url: str,
    limit: int = 5,
    cookie: Optional[str] = None,
) -> list[ChannelVideo]:
    """Trả về danh sách video mới nhất (theo create_time giảm dần) của kênh."""
    session = _new_session(cookie)
    url = extract_url(channel_url)
    sec_uid = _resolve_sec_uid(url, session)

    share_url = f"https://www.iesdouyin.com/share/user/{sec_uid}"
    try:
        resp = session.get(share_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise DouyinChannelError(f"Không tải được trang kênh: {e}") from e

    items = _parse_router_users_posts(resp.text)
    if not items:
        raise DouyinChannelError(
            f"Không tìm thấy bài đăng nào cho sec_uid={sec_uid} "
            "(kênh trống, riêng tư, hoặc cần cookie)."
        )
    videos = [_to_video(it) for it in items]
    # Mới nhất trước; item không có create_time đẩy xuống cuối.
    videos.sort(key=lambda v: v.create_time, reverse=True)
    return videos[: max(1, limit)]


if __name__ == "__main__":  # pragma: no cover - tiện test tay
    import sys

    if len(sys.argv) < 2:
        print("Dùng: python douyin_channel.py <link kênh douyin> [limit]")
        raise SystemExit(1)
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    vids = list_user_videos(sys.argv[1], limit=lim)
    print(json.dumps([v.to_dict() for v in vids], ensure_ascii=False, indent=2))
