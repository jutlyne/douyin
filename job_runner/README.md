# short-job-runner

Service Flask trung gian giữa n8n và Cloud Run Job `short-maker`.
Deploy: Cloud Run service `short-job-runner`
(`https://short-job-runner-YOUR_RUN_HASH-as.a.run.app`).

## Endpoints

| Method | Path | Mục đích |
|---|---|---|
| POST | `/run` | Kích hoạt job tạo Short từ 1 link Douyin (node **Create Short**). |
| GET | `/download` | Tải `final.mp4` bằng link ký HMAC (node **Download MP4**). |
| POST | `/channel/new-videos` | **MỚI** — liệt kê video chưa xử lý của 1 kênh Douyin. |
| POST | `/channel/mark` | **MỚI** — đánh dấu aweme_id đã xử lý (gọi sau khi upload xong). |

Tất cả yêu cầu header `X-API-Key: <API_KEY>`.

## Tự động hóa hoàn toàn (Schedule → đăng YouTube)

Thay **Telegram Trigger** bằng **Schedule Trigger** rồi nối:

```
Schedule Trigger (cron, vd 8h & 20h)
  → HTTP POST /channel/new-videos   { channel_url, limit: 2 }
  → IF new_count > 0
      → Split Out  videos[]
          → (mỗi video) HTTP POST /run
                { douyin_url: {{$json.douyin_url}}, callback_enabled: true,
                  callback_url: <Webhook URL>, chat_id: <telegram chat> }
          → ... job chạy, callback về Webhook (nhánh dưới) ...
          → Webhook → Download MP4 → Upload YouTube → Send Success
              → HTTP POST /channel/mark
                    { channel_url, aweme_id: {{$json.aweme_id}} }
```

- `/channel/new-videos` đã **tự lọc** các video đã có trong file state →
  chỉ trả video mới.
- Đánh dấu "đã xử lý" **sau khi upload YouTube thành công** (`/channel/mark`)
  để job lỗi vẫn được thử lại lần sau, không bị bỏ sót và không đăng trùng.
- Nếu muốn đơn giản (chấp nhận bỏ qua video khi job lỗi): gọi
  `/channel/new-videos` với `mark: true` để đánh dấu ngay khi trả, bỏ bước
  `/channel/mark`.

### `POST /channel/new-videos`

Body:

```json
{
  "channel_url": "https://www.douyin.com/user/MS4wLjABAAAA...",
  "limit": 2,
  "channel_key": "optional-stable-key",
  "state_uri": "optional gs://.../state.json",
  "mark": false
}
```

Trả:

```json
{
  "ok": true,
  "state_uri": "gs://YOUR_GCP_PROJECT-shorts/state/channels/<key>.json",
  "scanned": 5,
  "new_count": 2,
  "videos": [
    {"aweme_id": "73...", "desc": "...", "create_time": 1718000000,
     "duration_ms": 42000,
     "share_url": "https://www.douyin.com/video/73...",
     "douyin_url": "https://www.douyin.com/video/73..."}
  ]
}
```

`douyin_url` map thẳng vào field `douyin_url` của `/run`.

### `POST /channel/mark`

```json
{ "channel_url": "...", "aweme_ids": ["73...", "73..."] }
```

## Biến môi trường liên quan

| Env | Mặc định | Ý nghĩa |
|---|---|---|
| `CHANNEL_STATE_PREFIX` | `gs://YOUR_GCP_PROJECT-shorts/state/channels/` | Thư mục lưu file state mỗi kênh. |
| `CHANNEL_STATE_MAX` | `2000` | Số aweme_id tối đa giữ trong 1 file state. |
| `DOUYIN_COOKIE` | *(rỗng)* | Cookie Douyin nếu trang share đòi đăng nhập. |

## Test tay phần liệt kê kênh

```bash
python job_runner/douyin_channel.py "https://www.douyin.com/user/MS4wLjABAAAA..." 5
```

> Lưu ý: yt-dlp (2026.06) không có extractor `douyin:user` nên không liệt kê
> được kênh — module này dùng trang share iesdouyin (cùng kỹ thuật `_ROUTER_DATA`
> với `douyin_downloader.py`). Douyin có thể đổi cấu trúc; khi đó chỉnh
> `_parse_router_users_posts()`, hoặc nạp `DOUYIN_COOKIE`.
