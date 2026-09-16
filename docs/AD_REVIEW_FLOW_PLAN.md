# Kế hoạch: Flow cắt quảng cáo bán tự động qua Telegram (long pipeline)

> Bản kế hoạch triển khai. Đã được duyệt. Xem spec n8n chi tiết ở `docs/TELEGRAM_REVIEW_FLOW.md` (tạo cùng feature này).

## Context

Pipeline `douyin_shorts` tạo video long YouTube từ URL Douyin qua các Cloud Run job.
Hiện coordinator assemble xong là **tự upload YouTube ngay**, không cho người xem duyệt.
Khi verify một batch thực tế, phát hiện **quảng cáo lọt vào dub/sub** (đổi/thu điện thoại cũ,
vay tiền, brand promo…). Cần flow human-in-the-loop:

> `/long` chạy xong → **KHÔNG upload** (`pending_review`) → hệ thống dùng **Gemini** gợi ý mốc
> quảng cáo → user xác nhận qua Telegram (`cắt đoạn HH:MM:SS -> HH:MM:SS`) → job **cắt
> re-encode chính xác** + shift SRT/chapter/metadata → gửi preview → user `đăng ytb` → upload.

**Quyết định đã chốt:** detect QC bằng **Gemini**; cắt bằng **re-encode chính xác** (sub/dub
khớp đúng giây); tự deploy prod (chờ user test cuối trước khi chạy thật).

Mọi lệnh gcloud dùng `--configuration=develop --project=YOUR_GCP_PROJECT`, region `asia-southeast1`.

## Kiến trúc hiện tại

- `long-job-runner` (service): `POST /run` → ghi `request.json` → start `long-coordinator`.
  `GET /download` (signed). `GET /health`.
- `long-coordinator` (job) `_run()`: worker fanout → assemble → nếu `youtube_upload_enabled`
  thì `_run_youtube_upload` inline → ghi `status.json` → `_callback`.
- `long-assembler` (job): concat clip → `final-long.{mp4,srt,chapters.txt,json}`
  (`ffmpeg_ops.concat_mp4`, concat demuxer + `-c copy`).
- `long-youtube-uploader` (job): resumable upload từ GCS.
- **n8n workflow KHÔNG nằm trong repo** (self-hosted) → phần Telegram giao dạng spec.

## Thiết kế

Điều phối tập trung ở **coordinator** qua env `MODE` (`full` mặc định | `cut` | `upload`);
runner chỉ start coordinator với MODE khác nhau. ffmpeg cut tách thành **job mới `long-review`**.

1. **Persist subtitle có cấu trúc vào `final-long.json`** — `assembler_job.py`: ghi thêm
   `subtitles: [{start,end,text_vi}]` (globalized) để editor + detection đọc structured,
   không cần SRT parser.
2. **Detection Gemini (text-only)** — `gemini_long.py::detect_ad_spans(...)`: response_schema
   `AdDetectionResult{spans:[AdSpan{start,end,reason_vi,confidence}]}`, prompt nhận diện đoạn
   quảng cáo/PR sản phẩm, bỏ qua thoại truyện. Chạy trong coordinator (đã có `google-genai`).
3. **Job `long-review`** — `container_long/review_job.py` (mới). Env `OUTPUT_PREFIX`,
   `CUT_SPANS` (JSON `[[start,end],…]` giây). Tính kept segments; **re-encode chính xác** bằng
   helper mới `ffmpeg_ops.remove_spans(...)` (filter_complex trim/atrim+concat, crf 18, 1920×1080,
   aac); shift subtitle (drop trọn / clamp mép / trừ tổng đã cắt) → rewrite `.srt` +
   `subtitles` trong json; shift chapter; cập nhật `duration`; backup `final/pre-edit-<ts>/`.
   Dockerfile cần `ffmpeg`.
4. **Coordinator MODE + review-gate** — `coordinator_job.py`: thêm `REVIEW_JOB_NAME`, đọc `MODE`,
   cờ review (`review_mode` request / `LONG_REVIEW_MODE` env).
   - `full`: nếu review-mode & `youtube_upload_enabled` → `detect_ad_spans` → `status="pending_review"`
     + `ad_candidates` → callback `long.review.required`, **không upload**. Ngược lại giữ auto-upload.
   - `cut`: start `long-review` với `CUT_SPANS` → wait → re-detect → callback `long.cut.completed`.
   - `upload`: `_run_youtube_upload` → callback `long.youtube.completed`.
5. **Runner endpoints** — `long_job_runner/app.py`: `/run` passthrough `review_mode`;
   `POST /cut {batch_id, spans}` → coordinator `MODE=cut`; `POST /upload {batch_id}` →
   coordinator `MODE=upload`. Tách helper `_start_coordinator(env_overrides)`.
6. **Spec n8n** — `docs/TELEGRAM_REVIEW_FLOW.md`: render `long.review.required`; parse
   `cắt đoạn HH:MM:SS -> HH:MM:SS` → `/cut`; parse `đăng ytb` → `/upload`; payload contract.
7. **Tests** — `tests/test_long_pipeline.py`: kept-segment, subtitle shift, chapter shift,
   `detect_ad_spans` (mock client). Giữ 27 test cũ pass.
8. **Build + Deploy** — `Dockerfile.review` (+ffmpeg) + `cloudbuild.review.prod.yaml`; thêm build
   step vào `cloudbuild.prod.yaml`; `gcloud run jobs create long-review`; update coordinator
   (`REVIEW_JOB_NAME`, `LONG_REVIEW_MODE=true`, model env) + image mới; update runner service.
   Cập nhật `README.md` + `docs/DEPLOY.md`.

## Verification

1. `python -m unittest`/`pytest` toàn bộ test long pass (gồm test mới).
2. Editor cục bộ: mp4 ngắn + spans giả → ffprobe kiểm duration = duration - tổng cắt; SRT/chapter shift đúng.
3. `detect_ad_spans` bắt được mốc quảng cáo trên subtitle thật.
4. Prod dry-run: batch `review_mode=true` → `long.review.required` (không upload); `/cut` →
   `long.cut.completed` (sub/dub khớp); `/upload` → `long.youtube.completed`.
5. Backward-compat: `review_mode=false` vẫn auto-upload.

## Ghi chú
- Commit chỉ khi user duyệt. Không thêm dependency mới (genai + ffmpeg đã có).
- Fix gap-warning đang ở working tree — để commit chung khi user duyệt.
