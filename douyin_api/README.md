# Douyin Downloader API

API nhỏ (FastAPI) để resolve & tải video Douyin **không logo**. Bạn chỉ cần gửi URL Douyin, không cần cấu hình gì thêm.

## Cài đặt & chạy

```bash
cd douyin_api
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Mở docs tự động: http://127.0.0.1:8000/docs

## Endpoints (test bằng Postman)

| Method | URL | Mô tả |
|--------|-----|-------|
| GET | `/resolve?url=<link>` | Trả JSON metadata + link video không logo |
| POST | `/resolve` | Body JSON `{"url": "<link>"}` |
| GET | `/download?url=<link>` | Tải thẳng file `.mp4` về máy |
| GET | `/health` | Kiểm tra service sống |

### Ví dụ Postman

- **GET resolve**
  `GET http://127.0.0.1:8000/resolve?url=https://v.douyin.com/xxxxxxx/`
  Lưu ý: nếu URL chứa ký tự đặc biệt, để Postman tự encode (tab Params).

- **POST resolve**
  `POST http://127.0.0.1:8000/resolve`
  Body → raw → JSON:
  ```json
  { "url": "复制的整段分享文案，含 v.douyin.com 链接也được" }
  ```

- **Download**
  `GET http://127.0.0.1:8000/download?url=https://v.douyin.com/xxxxxxx/`
  Postman: nút **Send and Download** để lưu file.

### Mẫu phản hồi `/resolve`

```json
{
  "aweme_id": "7300000000000000000",
  "desc": "tiêu đề video",
  "author": "tên tác giả",
  "duration_ms": 15000,
  "cover": "https://...jpg",
  "play_url": "https://...mp4",
  "play_url_candidates": ["https://...mp4"],
  "music_url": "https://...mp3",
  "images": []
}
```

## Ghi chú

- Dùng endpoint chia sẻ `iesdouyin.com/share/video/{id}` (JSON nhúng trong `window._ROUTER_DATA`), **không cần** ký `a_bogus`/`X-Bogus`.
- Douyin thỉnh thoảng đổi cấu trúc JSON → có thể phải chỉnh `_parse_router_data()` trong `douyin_downloader.py`.
- Nội dung riêng tư / giới hạn vùng có thể cần truyền `cookie` (tham số tùy chọn).
- Có hỗ trợ post dạng **ảnh** (trường `images`); khi đó `/download` sẽ báo lỗi vì không có file mp4.
- Test nhanh không cần server: `python douyin_downloader.py <link>`.
```
