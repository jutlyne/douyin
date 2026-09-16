# Cover Visub Service

Tài liệu vận hành flow độc lập nhận URL Douyin và trả video đã che phụ đề Trung,
gắn phụ đề Việt và lồng tiếng Việt.

Cập nhật gần nhất: 2026-07-16.

## 1. Trạng thái production

| Thành phần | Giá trị |
|---|---|
| Project | `YOUR_GCP_PROJECT` |
| Region | `asia-southeast1` |
| Cloud Run Service | `desub-job-runner` |
| Service URL | `https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app` |
| Revision đã nghiệm thu | `desub-job-runner-00003-vh2` |
| Cloud Run Job | `cover-visub-lab` |
| Job image | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/desub-lab@sha256:df14926d5608b2340176bfb0de7d6eb67dff09ab9d65caeaab85b0b80b25db70` |
| Runner image | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/desub-runner:prod` |
| Pipeline version | `v9` |
| Scaling | Auto, Min `0`, Max `20` |
| Service concurrency | `20` |
| Job resource | CPU only, 4 CPU, 8 GiB |

Cloud Console:

- Service: `https://console.cloud.google.com/run/detail/asia-southeast1/desub-job-runner/metrics?project=YOUR_GCP_PROJECT`
- Job: `https://console.cloud.google.com/run/jobs/details/asia-southeast1/cover-visub-lab?project=YOUR_GCP_PROJECT`

## 2. Flow

```text
POST /run { douyin_url }
        |
        v
desub-job-runner
  - kiểm tra URL Douyin
  - tạo desub_id/prefix ổn định theo URL
  - chống chạy trùng khi job đang chạy
  - trả cache nếu URL đã hoàn tất
  - execute cover-visub-lab
        |
        v
cover-visub-lab (CPU only)
  - tải video Douyin
  - upload source.mp4 lên GCS
  - OCR band phụ đề Trung, tạo mask.json
  - Gemini tạo cue Việt
  - render rounded blur + phụ đề Việt
  - CapCut TTS BV075_streaming ở tốc độ 1.5
  - verify media/coverage/geometry/timing
  - upload output.mp4, cover_report.json và qa/
```

Flow này độc lập với Short và Long. Không route qua `job_runner` hoặc
`long_job_runner`.

## 3. Authentication và secret

Service cho phép request tới Cloud Run URL nhưng `/run` và `/status` bắt buộc có
header `X-API-Key`.

| Secret | Mục đích |
|---|---|
| `desub-runner-api-key` | Xác thực `/run` và `/status` |
| `desub-runner-download-secret` | Ký link HMAC `/download` |

Không ghi giá trị secret vào repo hoặc tài liệu. Lấy API key khi cần:

```powershell
$apiKey = (gcloud.cmd secrets versions access latest `
  --secret=desub-runner-api-key `
  --project=YOUR_GCP_PROJECT).Trim()
```

Tài khoản dùng để deploy/nghiệm thu:

```text
you@example.com
```

## 4. Gọi API

### Khởi chạy

```powershell
$headers = @{ 'X-API-Key' = $apiKey }
$body = @{
  douyin_url = 'https://v.douyin.com/omX_s0jMfi0/'
  force_refresh = $false
} | ConvertTo-Json

$run = Invoke-RestMethod `
  -Method Post `
  -Uri 'https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app/run' `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body $body

$run
```

Response mới thường là HTTP `202`:

```json
{
  "ok": true,
  "status": "queued",
  "cached": false,
  "desub_id": "douyin-967a16485ac8",
  "operation": "projects/.../operations/...",
  "status_url": "https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app/status?desub_id=douyin-967a16485ac8",
  "status_uri": "gs://.../status.json",
  "output_uri": "gs://.../output.mp4",
  "report_uri": "gs://.../cover_report.json",
  "download_url": "https://.../download?...",
  "expires_in": 86400
}
```

`download_url` có thời hạn 24 giờ. Nếu hết hạn, gọi `/status` để lấy link mới.

### Kiểm tra trạng thái

```powershell
$status = Invoke-RestMethod `
  -Uri $run.status_url `
  -Headers $headers

$status
```

Trạng thái có thể gặp:

```text
queued
validating
downloading | loading_source
detecting | loading_mask
translating | loading_cues
synthesizing
rendering
verifying
completed | failed
```

### Force refresh

Đặt `force_refresh=true` để chạy lại toàn bộ và không dùng mask/cues cũ:

```json
{
  "douyin_url": "https://v.douyin.com/example/",
  "force_refresh": true
}
```

Không gửi lại `force_refresh=true` khi cùng URL đang chạy vì yêu cầu này chủ ý
bỏ qua dedupe/cache.

## 5. GCS layout

Prefix được tạo ổn định từ URL đã chuẩn hóa:

```text
gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub/
  v9/
    douyin-<sha256-12>/
      source.mp4
      status.json
      mask.json
      visub_cues_draft.json
      visub_cues_final.json
      cue_quality_report.json
      speech_alignment.json
      output.mp4
      cover_report.json
      qa/*.png
```

Việc dùng prefix ổn định cho phép:

- request cùng URL đang chạy không tạo execution thứ hai;
- request cùng URL đã hoàn tất trả `cached=true` gần như ngay lập tức;
- mask/cues/source được tái sử dụng khi rerun thông thường.

## 6. Cấu hình Visub hiện tại

Các giá trị quan trọng trên `cover-visub-lab`:

```text
COVER_PIPELINE_VERSION=v9
COVER_AUTO_RESUME=true
COVER_DETECT_FPS=8
COVER_BAND_TOP_RATIO=0.66
COVER_CUE_MODEL=gemini-2.5-pro
DESUB_EASYOCR_GPU=false
VISUB_COVER_STYLE=blur
VISUB_COVER_RECT_MODE=per_event
VISUB_COVER_FILL=extend_text
VISUB_COVER_CORNER_RADIUS_PX=20
VISUB_COVER_EDGE_FEATHER_PX=3
DESUB_VISUB_OUTPUT_CRF=18
VISUB_TTS_ENABLED=true
VISUB_TTS_VOICE=BV075_streaming
VISUB_TTS_RESOURCE_ID=7102355803792740865
VISUB_TTS_RATE=1.5
VISUB_TTS_SHORT_SLOT_POLICY=fixed_rate_narration
VISUB_BGM_GAIN_DB=-20
```

Job vẫn hỗ trợ input GCS cũ bằng `COVER_SOURCE_URI`. Runner mới sử dụng
`COVER_DOUYIN_URL`; job yêu cầu đúng một trong hai input.

## 7. Kết quả nghiệm thu URL mẫu

Input:

```text
https://v.douyin.com/omX_s0jMfi0/
```

Execution:

```text
cover-visub-lab-jhqpg
```

Artifacts:

```text
gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub/v9/douyin-967a16485ac8/output.mp4
gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub/v9/douyin-967a16485ac8/cover_report.json
```

Kết quả:

| Kiểm tra | Kết quả |
|---|---|
| Execution | Thành công trong `1h13m45s` |
| Pipeline elapsed | `4404.800s` |
| Duration | `190.967s` |
| Duration delta | `0.000333s` |
| Video | H.264, 720x1280, 30 fps |
| Audio | AAC, 44.1 kHz, stereo |
| Cues/events | `77/77` |
| Clusters | `29` |
| Unmatched clusters | `0` |
| TTS voice clips | `77/77` |
| TTS Google fallback | `0` |
| Audio overlap | `0` |
| QA frames | `367` |
| Output size | `99,568,863` bytes |
| SHA-256 | `cc1748725ed4da518ed8b90d421bf8c7fd57143ec2722d4d72206bdc2388e567` |

Các gate `coverage`, `detection_timeline_coverage`, `text_coverage`, `geometry`,
`layout_quality`, `timing_quality` và `sub_band_rect_audit` đều pass.

Đã kiểm tra mắt các mốc `0.46`, `26.5`, `36.7`, `45.0`, `54.3`, `139.3` và
`185.5`: sub Trung được che, box đúng band đáy, chữ sản phẩm phía trên không bị
blur nhầm.

Lần đầu chậm chủ yếu do EasyOCR quét khoảng 1.528 frame trên CPU ở 8 fps. Các
lần gọi lại cùng URL sau khi hoàn tất trả cache ngay.

## 8. Build và deploy runner

Build context được giới hạn bằng file `.gcloudignore` riêng để không upload các
video QA trong workspace:

```powershell
gcloud.cmd builds submit `
  --project=YOUR_GCP_PROJECT `
  --config=desub_job_runner/cloudbuild.yaml `
  --ignore-file=desub_job_runner/.gcloudignore `
  .
```

Cloud Build chạy unit test API bên trong image trước khi push. Build nghiệm thu
`e01b9335-65c5-48b7-92a8-a57723364760` pass `7/7` test.

Deploy:

```powershell
gcloud.cmd run deploy desub-job-runner `
  --project=YOUR_GCP_PROJECT `
  --region=asia-southeast1 `
  --platform=managed `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/desub-runner:prod `
  --service-account=YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com `
  --allow-unauthenticated `
  --cpu=1 `
  --memory=512Mi `
  --concurrency=20 `
  --timeout=300 `
  --min-instances=0 `
  --max-instances=20 `
  --set-secrets=API_KEY=desub-runner-api-key:latest,DOWNLOAD_SECRET=desub-runner-download-secret:latest
```

Khi deploy lại phải giữ các env sau:

```text
GCP_PROJECT_ID=YOUR_GCP_PROJECT
RUN_REGION=asia-southeast1
JOB_NAME=cover-visub-lab
COVER_RESULT_ROOT=gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub
COVER_PIPELINE_VERSION=v9
DOWNLOAD_TTL_SECONDS=86400
RUNNING_STATUS_TTL_SECONDS=14400
SERVICE_BASE_URL=https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app
```

Script runtime của job hiện được nạp từ:

```text
gs://YOUR_GCP_PROJECT-media-sg/desub/_lab/tools/cover_visub_job.py
```

Sau khi sửa `experiments/desub/cover_visub_job.py`, phải upload lại file trên
trước khi execute job.

## 9. Kiểm tra và xử lý lỗi

Health check:

```powershell
Invoke-RestMethod `
  'https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app/health'
```

Đọc status trực tiếp:

```powershell
gcloud.cmd storage cat `
  gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub/v9/<desub_id>/status.json `
  --project=YOUR_GCP_PROJECT
```

Xem execution gần nhất:

```powershell
gcloud.cmd run jobs executions list `
  --job=cover-visub-lab `
  --region=asia-southeast1 `
  --project=YOUR_GCP_PROJECT `
  --limit=5
```

Xem log:

```powershell
gcloud.cmd logging read `
  'resource.type="cloud_run_job" AND resource.labels.job_name="cover-visub-lab"' `
  --project=YOUR_GCP_PROJECT `
  --freshness=2h `
  --limit=100 `
  --order=desc
```

Các lưu ý:

- `detecting` có thể giữ nguyên `updated_at` lâu vì EasyOCR chưa phát heartbeat
  theo frame. Kiểm tra trạng thái Cloud Run execution trước khi kết luận job treo.
- Video mẫu 191 giây từng mất 34–54 phút riêng cho OCR 8 fps.
- Nếu `download_url` hết hạn, không rerun job; gọi `/status` để lấy link mới.
- Nếu response link dùng `http://`, kiểm tra `SERVICE_BASE_URL`; revision hiện tại
  đã cố định HTTPS.
- Không cài thêm dependency trực tiếp trên máy. Runner kế thừa runtime dependency
  đã deploy và Cloud Build kiểm tra API trong container.

## 10. File liên quan

```text
desub_job_runner/app.py
desub_job_runner/core.py
desub_job_runner/Dockerfile
desub_job_runner/cloudbuild.yaml
desub_job_runner/.gcloudignore
desub_job_runner/README.md
experiments/desub/cover_visub_job.py
tests/test_desub_job_runner.py
tests/test_cover_visub.py
docs/COVER_VISUB_SERVICE.md
```

Các thay đổi chưa được commit tại thời điểm cập nhật tài liệu này.
