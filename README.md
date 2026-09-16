# douyin_shorts

Pipeline tạo video tiếng Việt từ Douyin, chạy trên Google Cloud Run và được điều phối bằng n8n/Telegram.

Repo hiện có hai flow chính:

- `short`: nhận 1 link Douyin, tạo YouTube Short 9:16. n8n vẫn tải file MP4 rồi upload bằng YouTube node.
- `long`: nhận 1-30 link Douyin, xử lý từng source độc lập, cache source đã thành công trên GCS, ghép thành video 16:9, và có thể upload trực tiếp lên YouTube bằng Cloud Run Job để tránh n8n phải tải file lớn.

## Thành phần chính

```text
douyin_shorts/
├── douyin_api/               # Resolver/downloader Douyin
├── container_short/          # Cloud Run Job short-maker
├── job_runner/               # Cloud Run service short-job-runner
├── container_long/           # long-worker, long-assembler, long-coordinator, uploader
├── long_job_runner/          # Cloud Run service long-job-runner
├── tests/                    # Unit tests cho checkpoint/cache/long logic
└── docs/DEPLOY.md            # Tài liệu rebuild/deploy prod
```

## Long flow hiện tại

1. Telegram/n8n gửi `/long` với 1-30 URL.
2. `long-job-runner` validate payload, ghi `request.json` vào media bucket và start `long-coordinator`.
3. `long-coordinator` normalize URL và check source cache theo `{OUTPUT_ROOT}/_source-cache/{LONG_SOURCE_CACHE_VERSION}/{url_hash}/final.*`.
4. URL cache hit được copy vào batch hiện tại, không chạy Gemini lại.
5. URL chưa cache được chạy bằng từng execution `long-worker`, fanout mặc định `2`, mỗi source retry tối đa 1 lần (`WORKER_SOURCE_MAX_ATTEMPTS=2`).
6. Nếu còn source fail sau retry, coordinator không assemble và callback fail kèm `failed_sources`.
7. Nếu tất cả source OK/cache hit, coordinator tạo `manifest.json`, chạy `long-assembler`, rồi upload YouTube bằng `long-youtube-uploader` nếu `YOUTUBE_UPLOAD_ENABLED=true`.
8. Callback thành công trả `event=long.youtube.completed`, `youtube_url`, `output_uri`, `metadata_uri`, `subtitle_uri`.

## Payload long

String list cũ vẫn hoạt động:

```json
{
  "videos": ["https://v.douyin.com/example/"]
}
```

Object list hỗ trợ force refresh từng URL:

```json
{
  "force_refresh": false,
  "videos": [
    { "douyin_url": "https://v.douyin.com/one/" },
    { "douyin_url": "https://v.douyin.com/two/", "force_refresh": true }
  ]
}
```

Quy ước Telegram/n8n:

```text
/long <url>                         # tạo 1 video long riêng
/long <url1> <url2> ...             # ghép nhiều source
/long --refresh <url1> <url2>       # bỏ cache toàn bộ
/long <url1> !<url2> <url3>         # chỉ chạy lại url2
```

## YouTube metadata

Long upload dùng YouTube category `24` (Entertainment), privacy `private`, `made_for_kids=false`.

Public hashtag line đang cố định để tránh YouTube tách hashtag có dấu cách:

```text
#truyentranhreview #tutien #truongsinh #tomtatphim #manhua #huyenhuyen #douyin #china #hanhan
```

## Cấu hình

Repo này dùng placeholder cho mọi định danh hạ tầng thật (`YOUR_GCP_PROJECT`, `YOUR_PROJECT_NUMBER`, `YOUR_RUN_HASH`, `YOUR_TELEGRAM_CHAT_ID`, ...). Trước khi build/deploy, bạn cần thay các placeholder này bằng giá trị GCP project, số project, Cloud Run URL hash, chat id Telegram... của riêng bạn (grep toàn repo để tìm hết các chỗ dùng).

Danh sách đầy đủ biến môi trường mà từng service đọc được liệt kê ở [.env.example](.env.example) (copy thành `.env` rồi điền giá trị thật). Biến đánh dấu `# required` là bắt buộc phải set, thiếu sẽ báo lỗi rõ ràng khi service khởi động/chạy job; các biến còn lại là tuỳ chọn, dùng default ghi trong comment nếu bỏ trống.

## Deploy

Xem hướng dẫn đầy đủ ở [docs/DEPLOY.md](docs/DEPLOY.md).

Các lệnh nhanh thường dùng:

```powershell
# Build toàn bộ long prod images
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=container_long\cloudbuild.prod.yaml .

# Unit test quan trọng cho checkpoint/cache/uploader env
python -m unittest tests.test_long_batch_checkpoint
```

## n8n hiện tại

Cloud Run service:

```text
n8n-selfhost
https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app
```

Callback workflow long/short đang dùng:

```text
https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app/webhook/short-done
```

Sau khi import workflow JSON, cần reconnect credential Telegram/YouTube và activate workflow.

## n8n manual recovery

`n8n-recovery` là Cloud Run service nhẹ nằm ngoài n8n để tự phục hồi khi n8n process còn sống nhưng DB connection pool bị kẹt. Service có hai endpoint token-protected:

```text
/status?token=...   # kiểm tra n8n/rest/push
/restart?token=...  # ép Cloud Run rollout lại n8n-selfhost
```

URL recovery hiện tại:

```text
https://n8n-recovery-YOUR_RUN_HASH-as.a.run.app
```

Khi gặp lỗi `503 Database is not ready`, mở `/restart?token=...` để ép n8n reconnect Postgres. Không còn Cloud Scheduler chạy định kỳ.
## Test và kiểm tra sản phẩm

Kiểm tra job/execution:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs executions list --job=long-coordinator --region=asia-southeast1 --limit=10
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs executions list --job=long-worker --region=asia-southeast1 --limit=10
```

Kiểm tra output GCS:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT storage ls --long gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/final/
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT storage cat gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/status.json
```

## Ghi chú vận hành

- Nhánh chính của repo là `main`.
- Long xử lý trực tiếp ở prod theo quyết định hiện tại.
- Khi đăng từng tập trước khi verify YouTube phone, dùng `/long <url>` từng URL. Khi verify xong, gửi lại cùng danh sách URL để gộp full; hệ thống sẽ reuse source cache.
- Không dùng `--refresh` nếu muốn tận dụng cache.
