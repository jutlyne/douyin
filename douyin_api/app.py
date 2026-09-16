"""FastAPI service tải video Douyin.

Chạy:
    cd douyin_api
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000

Test bằng Postman:
    GET  http://127.0.0.1:8000/resolve?url=<link douyin>     -> JSON metadata + link không logo
    GET  http://127.0.0.1:8000/download?url=<link douyin>     -> tải thẳng file .mp4
    POST http://127.0.0.1:8000/resolve   body JSON {"url": "<link>"}

Chỉ cần gửi URL Douyin, không cần cấu hình gì thêm.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from douyin_downloader import DouyinError, open_stream, resolve

app = FastAPI(
    title="Douyin Downloader API",
    description="Resolve & tải video Douyin không logo. Chỉ cần gửi URL.",
    version="1.0.0",
)

_FILENAME_SAFE = re.compile(r"[^\w\-. ]+")


class ResolveBody(BaseModel):
    url: str
    cookie: str | None = None


@app.get("/")
def root():
    return {
        "service": "douyin-downloader",
        "endpoints": {
            "GET /resolve?url=...": "Trả metadata + link video không logo",
            "POST /resolve": 'body {"url": "..."}',
            "GET /download?url=...": "Tải thẳng file mp4 về",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def _resolve_or_400(url: str, cookie: str | None):
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="Thiếu tham số 'url'.")
    try:
        return resolve(url, cookie=cookie)
    except DouyinError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - trả lỗi gọn cho client
        raise HTTPException(status_code=500, detail=f"Lỗi nội bộ: {e}") from e


@app.get("/resolve")
def resolve_get(
    url: str = Query(..., description="Link hoặc đoạn text chia sẻ Douyin"),
    cookie: str | None = Query(None, description="(Tùy chọn) cookie nếu nội dung bị giới hạn"),
):
    return _resolve_or_400(url, cookie).to_dict()


@app.post("/resolve")
def resolve_post(body: ResolveBody):
    return _resolve_or_400(body.url, body.cookie).to_dict()


@app.get("/download")
def download(
    url: str = Query(..., description="Link hoặc đoạn text chia sẻ Douyin"),
    cookie: str | None = Query(None),
):
    info = _resolve_or_400(url, cookie)
    if not info.play_url:
        raise HTTPException(status_code=422, detail="Không lấy được link video (có thể là post ảnh).")

    try:
        upstream = open_stream(info.play_url, cookie=cookie)
    except DouyinError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    safe_desc = _FILENAME_SAFE.sub("", info.desc).strip()[:60] or info.aweme_id
    filename = f"{safe_desc}.mp4"

    def iterfile():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {
        # RFC 5987: hỗ trợ tên file Unicode (tiếng Trung/Việt).
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    return StreamingResponse(iterfile(), media_type="video/mp4", headers=headers)
