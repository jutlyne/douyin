# Telegram ad-review flow (long pipeline)

Human-in-the-loop flow: sau khi job long assemble xong, **không upload ngay**.
Hệ thống gợi ý mốc quảng cáo (Gemini), người dùng xác nhận cắt qua Telegram, cắt
xong mới upload. Backend đã implement trong repo này; phần dưới là hợp đồng API +
spec để dựng workflow n8n (n8n nằm ngoài repo).

```
/long → coordinator (assemble) ──(LONG_REVIEW_MODE=true)──▶ long.review.required
                                                              │  (ad_candidates + preview)
        Telegram: "cắt đoạn 01:29:52 -> 01:30:10" ─▶ POST /cut ─▶ long.cut.completed
                                                              │  (preview mới + candidates còn lại)
        Telegram: "đăng ytb" ───────────────────────▶ POST /upload ─▶ long.youtube.completed
```

Bật/tắt: đặt env coordinator `LONG_REVIEW_MODE=true` (mặc định prod), hoặc gửi
`review_mode: true|false` trong payload `/run` cho từng batch. Khi tắt → giữ hành
vi cũ (assemble xong auto-upload).

## Runner endpoints

Base URL: service `long-job-runner`. Mọi request cần header `X-API-Key: <LONG_RUNNER_API_KEY>`.

### `POST /run` — tạo batch (đã có, thêm 1 field)

```json
{
  "id": "telegram-<chat>-<msg>-<ts>",
  "chat_id": "YOUR_TELEGRAM_CHAT_ID",
  "callback_enabled": true,
  "callback_url": "https://n8n-selfhost-.../webhook/long-event",
  "review_mode": true,
  "youtube_upload_enabled": true,
  "youtube_privacy_status": "private",
  "videos": [{ "douyin_url": "https://v.douyin.com/xxxx/" }]
}
```

`review_mode` là tùy chọn; nếu bỏ trống thì lấy theo env coordinator.

### `POST /cut` — cắt các đoạn quảng cáo đã xác nhận

```json
{
  "batch_id": "telegram-YOUR_TELEGRAM_CHAT_ID-201-1783471754131",
  "spans": [["01:29:52", "01:30:10"], ["00:12:12", "00:12:20"]]
}
```

- `batch_id` (hoặc `id`): batch cần cắt — chính là `id` đã gửi ở `/run`.
- `spans`: danh sách đoạn cần **bỏ khỏi video**. Mỗi đoạn là `["start","end"]`.
  Chấp nhận `HH:MM:SS`, `MM:SS`, `SS`, hoặc số giây; hoặc `{ "start":.., "end":.. }`.
- Trả `202 {ok, status:"cutting", batch_id, spans, operation, status_uri}`.
- Có thể gọi `/cut` nhiều lần liên tiếp (cắt thêm) — mỗi lần thao tác trên bản mới nhất.

### `POST /upload` — duyệt xong, upload YouTube

```json
{ "batch_id": "telegram-YOUR_TELEGRAM_CHAT_ID-201-1783471754131" }
```

Trả `202 {ok, status:"uploading", batch_id, operation, status_uri}`.

### `POST /review` — detect lại quảng cáo (không dựng lại)

```json
{ "batch_id": "telegram-YOUR_TELEGRAM_CHAT_ID-201-1783471754131" }
```

Chạy lại Gemini detect trên `final-long.json` đã có → phát lại `long.review.required`. Rẻ/nhanh
(không assemble). Dùng cho ops/debug hoặc khi muốn xin lại gợi ý mốc QC.

## Callback events (POST tới `callback_url`)

| event | status | Ý nghĩa | Field chính |
|---|---|---|---|
| `long.review.required` | `pending_review` | Assemble xong, chờ duyệt QC | `ad_candidates`, `download_url`, `subtitle_uri`, `duration` |
| `long.cut.completed` | `pending_review` | Đã cắt xong 1 lượt, chờ duyệt tiếp | `applied_cuts`, `ad_candidates`, `download_url`, `duration` |
| `long.youtube.completed` | `uploaded` | Đã upload YouTube | `youtube_video_id`, `youtube_url` |
| `long.youtube.failed` | `upload_failed` | Upload lỗi | `error` |
| `long.batch.failed` | `failed` | Batch lỗi (source/…) | `error`, `failed_sources` |

`ad_candidates` (gợi ý — người dùng vẫn là chốt cuối):

```json
"ad_candidates": [
  {
    "start": 5392.0, "end": 5410.0,
    "start_hms": "01:29:52", "end_hms": "01:30:10",
    "reason_vi": "Quảng cáo thu cũ đổi mới điện thoại",
    "confidence": 0.92
  }
]
```

`download_url` là link preview có chữ ký (TTL ~12h) để xem/tải bản hiện tại trước khi quyết định.

## Spec workflow n8n

1. **Telegram Trigger** (bot) + **Webhook** nhận callback từ coordinator (path `long-event`).

2. **Khi nhận `long.review.required` / `long.cut.completed`** → gửi Telegram cho `chat_id`:
   - Link preview (`download_url`).
   - Bảng mốc QC từ `ad_candidates`, ví dụ mỗi dòng: `• 01:29:52 → 01:30:10 — Quảng cáo thu cũ đổi mới (0.92)`.
   - Nhắc cú pháp: gửi `cắt đoạn HH:MM:SS -> HH:MM:SS` (nhiều đoạn cách nhau bằng dấu `;` hoặc xuống dòng), hoặc `đăng ytb` để đăng.

3. **Parse tin nhắn Telegram** (node Code/Function):
   - **Lệnh cắt**: regex bắt tất cả cặp thời gian, gợi ý:
     ```
     /(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})\s*(?:->|→|-|đến|den|tới|toi)\s*(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})/g
     ```
     Kích hoạt khi tin nhắn chứa "cắt"/"cat". Gom thành `spans: [["h:m:s","h:m:s"], ...]` → `POST /cut` với `batch_id` (lấy từ context hội thoại / reply).
   - **Lệnh đăng**: khớp `/\b(đăng|dang)\b.*\b(ytb|youtube|yt)\b/i` (hoặc chỉ `đăng`) → `POST /upload` với `batch_id`.

4. **Lưu `batch_id` theo `chat_id`** (n8n static data / DB) để biết tin nhắn "cắt/đăng" áp cho batch nào; hoặc yêu cầu người dùng **reply** vào tin `long.review.required` và đọc `batch_id` từ tin gốc.

5. **Khi nhận `long.youtube.completed`** → gửi `youtube_url` cho người dùng, kết thúc.
   Khi nhận `*.failed` → gửi `error`.

## Ghi chú kỹ thuật

- Cắt là **re-encode chính xác theo giây** (`ffmpeg` filter trim+concat), sub/lồng
  tiếng shift khớp; bản cũ được backup ở `gs://.../<batch>/final/pre-edit-<ts>/`.
- Cắt xong coordinator **chạy lại Gemini detect** trên sub đã shift, nên
  `ad_candidates` trong `long.cut.completed` là các mốc **còn lại** (theo timeline mới).
- Endpoint `/cut`, `/upload` bất đồng bộ (trả `202`); kết quả về qua callback.
