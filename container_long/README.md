# Long video pipeline

Pipeline long tạo video 16:9 từ 1-30 URL Douyin. Mỗi URL được xử lý độc lập để tăng độ ổn định, cache source thành công trên GCS, rồi assembler ghép các source theo thứ tự request.

## Cloud Run components

| Component | Type | Prod image | Nhiệm vụ |
|---|---|---|---|
| `long-job-runner` | service | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-runner:prod` | HTTP entrypoint cho n8n, ghi request và start coordinator |
| `long-coordinator` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-coordinator:prod` | cache/checkpoint/retry source, assemble, callback, start uploader |
| `long-worker` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-worker:prod` | xử lý đúng 1 URL Douyin thành `final.mp4/json/srt` |
| `long-assembler` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-assembler:prod` | nối source đã xử lý, tạo final video + chapters + SRT |
| `long-youtube-uploader` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-youtube-uploader:prod` | upload final MP4 từ GCS lên YouTube bằng resumable upload |
| `long-thumbnail-generator` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-thumbnail-generator:prod` | tạo artwork 16:9 bằng Vertex AI, chèn chữ Part deterministic và lưu JPEG dưới 2 MB |
| `long-review` | job | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-review:prod` | cắt các đoạn quảng cáo đã xác nhận khỏi final video (re-encode) + shift SRT/chapter/metadata |

## Buckets

```text
Media/output root:  gs://YOUR_GCP_PROJECT-media-sg/long
Scratch/work root:  gs://YOUR_GCP_PROJECT-scratch-sg/long
Source cache:       gs://YOUR_GCP_PROJECT-media-sg/long/_source-cache/v4/<url_hash>/final.*
```

Source cache nằm trong media bucket để lần sau gửi lại cùng URL vẫn reuse được. Scratch bucket dùng cho artifact tạm của batch hiện tại.

## Request schema

`long-job-runner` nhận payload:

```json
{
  "id": "telegram-YOUR_TELEGRAM_CHAT_ID-178-1783057638376",
  "chat_id": "YOUR_TELEGRAM_CHAT_ID",
  "callback_enabled": true,
  "callback_url": "https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app/webhook/short-done",
  "youtube_upload_enabled": true,
  "youtube_privacy_status": "private",
  "youtube_category_id": "24",
  "youtube_made_for_kids": false,
  "series_part_number": 3,
  "thumbnail_generate_enabled": true,
  "thumbnail_required": true,
  "thumbnail_reference_uri": "gs://YOUR_GCP_PROJECT-media-sg/long/_assets/ha-nhan-thumbnail-reference.png",
  "videos": [
    { "douyin_url": "https://v.douyin.com/example/" }
  ]
}
```

`videos` nhận từ 1 đến 30 item. Item có thể là string URL hoặc object `{ "douyin_url": "...", "force_refresh": true }`.

## Cache và retry

- URL được normalize rồi hash cùng `LONG_SOURCE_CACHE_VERSION`.
- `force_refresh=true` ở top-level chạy lại toàn bộ source.
- `force_refresh=true` ở từng item chỉ chạy lại item đó.
- Prod hiện dùng `LONG_SOURCE_CACHE_VERSION=v4`.
- Prod hiện dùng `WORKER_FANOUT=2`.
- Prod hiện dùng `WORKER_SOURCE_MAX_ATTEMPTS=2`, tức chạy lần đầu + retry 1 lần.
- Nếu còn source fail sau retry, batch không assemble; `status.json` có `failed_sources` gồm index, URL, error, attempts.

## Worker timing hiện tại

Prod worker đang override chunk config như sau:

```text
LONG_CHUNK_SECONDS=90
LONG_MIN_CHUNK_SECONDS=60
LONG_MAX_CHUNK_SECONDS=120
LONG_ANALYSIS_PADDING_SECONDS=3
LONG_VISUAL_REFRESH=auto
LONG_VIDEO_CRF=18
LONG_VIDEO_WIDTH=1920
LONG_VIDEO_HEIGHT=1080
```

Các giá trị này quan trọng cho chất lượng phụ đề/lồng tiếng ở video nhiều đoạn. Khi rebuild/redeploy, giữ các env trên nếu không có lý do đổi.

## Output

Final batch output:

```text
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/request.json
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/manifest.json
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/status.json
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/final-long.mp4
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/final-long.json
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/final-long.srt
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/final-long.chapters.txt
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/youtube-upload.json
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/youtube-thumbnail.jpg
gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/youtube-thumbnail-generation.json
```

Callback success khi upload YouTube bật:

```json
{
  "event": "long.youtube.completed",
  "ok": true,
  "status": "uploaded",
  "youtube_upload_status": "uploaded",
  "youtube_video_id": "...",
  "youtube_url": "https://www.youtube.com/watch?v=..."
}
```

Callback success khi upload YouTube tắt:

```json
{
  "event": "long.batch.completed",
  "ok": true,
  "status": "completed",
  "download_url": "https://long-job-runner.../download?..."
}
```

## Ad-review flow (human-in-the-loop)

Khi coordinator chạy với `LONG_REVIEW_MODE=true` (hoặc request có `review_mode=true`),
sau khi assemble nó **không upload ngay** mà dùng Gemini gợi ý mốc quảng cáo và gửi
callback `long.review.required`. Người dùng xác nhận cắt qua Telegram → runner
`POST /cut` (coordinator `MODE=cut` → job `long-review` cắt + shift sub/chapter →
callback `long.cut.completed`), rồi `POST /upload` (coordinator `MODE=upload`).
`final-long.json` giờ có thêm field `subtitles` (timeline có cấu trúc) cho detect + cut.

Chi tiết hợp đồng API + spec n8n: `docs/TELEGRAM_REVIEW_FLOW.md`.

## YouTube uploader

`long-youtube-uploader` đọc video từ GCS và upload theo chunk, không tải file qua n8n.

Prod env:

```text
YOUTUBE_PRIVACY_STATUS=private
YOUTUBE_CATEGORY_ID=24
YOUTUBE_MADE_FOR_KIDS=false
YOUTUBE_NOTIFY_SUBSCRIBERS=false
YOUTUBE_UPLOAD_CHUNK_BYTES=67108864
YOUTUBE_CHUNK_TIMEOUT_SECONDS=900
YOUTUBE_UPLOAD_MAX_RETRIES=8
```

Secrets cần có trong Secret Manager và được mount vào job:

```text
youtube-client-id       -> YOUTUBE_CLIENT_ID
youtube-client-secret   -> YOUTUBE_CLIENT_SECRET
youtube-refresh-token   -> YOUTUBE_REFRESH_TOKEN
```

Không commit secret values vào repo.

## Build

Prod build all long images:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=container_long\cloudbuild.prod.yaml .
```

Test build all long images:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=container_long\cloudbuild.test.yaml .
```

## Deploy

Chi tiết từng lệnh update service/job nằm ở `docs/DEPLOY.md`.
