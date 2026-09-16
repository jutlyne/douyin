# Di chuyển `douyin_shorts` sang môi trường GCP mới (giữ nguyên source cache)

Mục tiêu: chuyển toàn bộ pipeline long (và runner) sang một **project GCP mới**, **giữ lại
source cache** đã xử lý ở env cũ để không phải chạy lại (download + Gemini + TTS + render) hàng
chục link — vừa tốn tiền vừa dễ đụng quota Gemini 429.

> **Nguyên tắc cache (vì sao migrate được):** cache lưu ở
> `gs://<media-bucket>/long/_source-cache/<version>/<url_hash>/final.{mp4,json,srt}`, với
> `url_hash = sha256(normalized_url)[:32]`. Key **chỉ gồm URL đã normalize + `LONG_SOURCE_CACHE_VERSION`**,
> **không dính project/bucket**. Nên chỉ cần **copy nguyên prefix `_source-cache/` sang bucket media
> mới đúng đường dẫn tương đối**, và **giữ nguyên version** là cache hit lại bình thường. Không cần
> sửa code.

---

## 0. Điền thông số (dùng xuyên suốt)

| Biến | Env CŨ | Env MỚI (điền) |
|---|---|---|
| Project ID | `YOUR_GCP_PROJECT` | `<NEW_PROJECT>` |
| Region (Cloud Run) | `asia-southeast1` | `<NEW_REGION>` |
| Artifact Registry repo | `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin` | `<NEW_AR_REPO>` |
| Media bucket | `YOUR_GCP_PROJECT-media-sg` | `<NEW_MEDIA>` |
| Scratch bucket | `YOUR_GCP_PROJECT-scratch-sg` | `<NEW_SCRATCH>` |
| Vertex region (Gemini) | `global` | `global` (giữ nguyên) |
| Cache version | `v4` | **`v4` (BẮT BUỘC giữ nguyên)** |

Auth cho project mới (tạo config gcloud riêng để khỏi lẫn với `develop`):

```powershell
gcloud.cmd config configurations create newenv
gcloud.cmd config set project <NEW_PROJECT>
gcloud.cmd auth login          # account có quyền trên project mới
gcloud.cmd auth application-default login
```

Các lệnh dưới ghi `--project=<NEW_PROJECT>` cho rõ; thêm `--configuration=newenv` nếu muốn.

---

## 1. Chuẩn bị project mới

```powershell
# Bật API
gcloud.cmd --project=<NEW_PROJECT> services enable `
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  storage.googleapis.com aiplatform.googleapis.com secretmanager.googleapis.com

# Artifact Registry (docker) — đặt tên repo khớp <NEW_AR_REPO>
gcloud.cmd --project=<NEW_PROJECT> artifacts repositories create douyin `
  --repository-format=docker --location=us

# Buckets (cùng miền với region để rẻ/nhanh; bật uniform access)
gcloud.cmd --project=<NEW_PROJECT> storage buckets create gs://<NEW_MEDIA>   --location=<REGION_HOẶC_MULTI> --uniform-bucket-level-access
gcloud.cmd --project=<NEW_PROJECT> storage buckets create gs://<NEW_SCRATCH> --location=<REGION_HOẶC_MULTI> --uniform-bucket-level-access
```

Service account chạy job (hoặc dùng compute default SA của project mới) cần các role:
`roles/run.admin` (deploy), `roles/run.invoker` (coordinator gọi job khác),
`roles/storage.objectAdmin` (đọc/ghi 2 bucket), `roles/aiplatform.user` (Gemini/Vertex),
`roles/secretmanager.secretAccessor` (uploader), `roles/logging.logWriter`.

---

## 2. ⭐ Chuyển SOURCE CACHE (phần quan trọng nhất)

Cache nằm dưới bucket media, prefix `long/_source-cache/`. Copy sang bucket media mới **giữ đúng
đường dẫn tương đối**:

```powershell
# rsync resumable + idempotent (chạy lại được nếu đứt). Cache có thể vài chục GB.
gcloud.cmd --project=<NEW_PROJECT> storage rsync -r `
  gs://YOUR_GCP_PROJECT-media-sg/long/_source-cache `
  gs://<NEW_MEDIA>/long/_source-cache
```

Kiểm tra sau khi copy (số object phải khớp, khác nhau ~0 là ổn):

```powershell
gcloud.cmd --project=YOUR_GCP_PROJECT storage ls -r "gs://YOUR_GCP_PROJECT-media-sg/long/_source-cache/v4/**" | Measure-Object -Line
gcloud.cmd --project=<NEW_PROJECT>   storage ls -r "gs://<NEW_MEDIA>/long/_source-cache/v4/**"          | Measure-Object -Line
```

**Điều kiện để cache hit lại ở env mới:**
- Coordinator env mới đặt `LONG_SOURCE_CACHE_VERSION=v4` (KHÔNG bump `v5`).
- `OUTPUT_ROOT` env mới = `gs://<NEW_MEDIA>/long` (vì cache root suy ra từ đây → sẽ trỏ vào
  `gs://<NEW_MEDIA>/long/_source-cache/v4/`, đúng chỗ vừa copy).
- Không sửa hàm `_normalize_url` (giữ hash khớp).

**Quyền cho lệnh copy:** account chạy `rsync` phải **đọc được bucket cũ** và **ghi được bucket
mới**. Nếu 2 project khác nhau, dùng 1 account có quyền cả hai, hoặc grant tạm
`roles/storage.objectViewer` cho account đó trên bucket cũ.

> ⚠️ **Cache KHÔNG hash theo cấu hình xử lý** (chunk seconds, `LONG_VIDEO_WIDTH/HEIGHT/CRF`, giọng
> TTS, prompt Gemini). Nếu env mới **đổi các thông số chất lượng đó**, bản cache cũ vẫn bị tái dùng
> → trộn cũ/mới. Khi đó **bump `LONG_SOURCE_CACHE_VERSION` (v4→v5)** để buộc xử lý lại (và bỏ qua
> bước copy cache ở trên vì sẽ không dùng nữa).

*(Tùy chọn)* copy luôn output các batch cũ nếu muốn giữ lịch sử:
`gcloud.cmd ... storage rsync -r gs://YOUR_GCP_PROJECT-media-sg/long gs://<NEW_MEDIA>/long` — nặng và
thường không cần; chỉ `_source-cache` là đủ để tiết kiệm xử lý.

---

## 3. Build + push images sang Artifact Registry mới

Các file cloudbuild hard-code registry `us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin`. Đổi sang repo mới
rồi build. Các chỗ cần đổi `YOUR_GCP_PROJECT/douyin` → `<NEW_PROJECT>/<REPO>`:

- `container_long/cloudbuild.prod.yaml` (và các `cloudbuild.*.prod.yaml`)
- `long_job_runner/cloudbuild.prod.yaml`
- *(khuyến nghị)* các `Dockerfile*` ENV mặc định: `GCP_PROJECT_ID`, `SCRATCH_GS_PREFIX`,
  `OUTPUT_ROOT`, `SCRATCH_ROOT` (không bắt buộc vì sẽ override lúc deploy, nhưng đổi cho sạch).

Build toàn bộ image long (worker/assembler/coordinator/runner/uploader/review):

```powershell
gcloud.cmd --project=<NEW_PROJECT> builds submit --config=container_long/cloudbuild.prod.yaml .
```

*(Thay thế)* nếu muốn khỏi rebuild, có thể copy image cũ → repo mới bằng `gcrane cp`/`crane cp`
(cần cài crane). Rebuild sạch hơn và đảm bảo ENV mặc định đúng project mới.

---

## 4. Secrets (cho uploader YouTube)

Tạo lại trong project mới rồi mount như env cũ:

```powershell
foreach ($s in "youtube-client-id","youtube-client-secret","youtube-refresh-token") {
  gcloud.cmd --project=<NEW_PROJECT> secrets create $s --replication-policy=automatic
  gcloud.cmd --project=<NEW_PROJECT> secrets versions add $s --data-file="$env:USERPROFILE\Downloads\$s.txt"
}
```

Nếu runner dùng `API_KEY`/`DOWNLOAD_SECRET` dạng secret thì tạo tương tự (hoặc set thẳng env khi deploy).

---

## 5. Deploy jobs + service (override env → project/bucket MỚI)

Nguyên tắc: lấy đúng bộ env như `docs/DEPLOY.md`, và **override** mọi thứ trỏ về env cũ:
`GCP_PROJECT_ID`, `RUN_REGION`, `OUTPUT_ROOT`, `SCRATCH_ROOT`, `SCRATCH_GS_PREFIX`, các `*_JOB_NAME`,
`SERVICE_BASE_URL`. **Giữ `LONG_SOURCE_CACHE_VERSION=v4`.**

```powershell
$P="<NEW_PROJECT>"; $R="<NEW_REGION>"; $IMG="<NEW_AR_REPO>"
$MEDIA="gs://<NEW_MEDIA>/long"; $SCRATCH="gs://<NEW_SCRATCH>/long"

# long-worker
gcloud.cmd --project=$P run jobs create long-worker --region=$R --image=$IMG/long-worker:prod `
  --cpu=4 --memory=16Gi --task-timeout=43200s --max-retries=2 `
  --set-env-vars=GCP_PROJECT_ID=$P,VERTEX_REGION=global,GEMINI_MODEL=gemini-2.5-pro,SCRATCH_GS_PREFIX=$SCRATCH,LONG_CHUNK_SECONDS=90,LONG_MIN_CHUNK_SECONDS=60,LONG_MAX_CHUNK_SECONDS=120,LONG_ANALYSIS_PADDING_SECONDS=3,LONG_VISUAL_REFRESH=auto,LONG_VIDEO_CRF=18,LONG_VIDEO_WIDTH=1920,LONG_VIDEO_HEIGHT=1080

# long-assembler
gcloud.cmd --project=$P run jobs create long-assembler --region=$R --image=$IMG/long-assembler:prod `
  --cpu=4 --memory=16Gi --task-timeout=43200s --max-retries=0 `
  --set-env-vars=GCP_PROJECT_ID=$P,VERTEX_REGION=global,GEMINI_MODEL=gemini-2.5-pro

# long-review (job cắt quảng cáo)
gcloud.cmd --project=$P run jobs create long-review --region=$R --image=$IMG/long-review:prod `
  --cpu=4 --memory=16Gi --task-timeout=43200s --max-retries=0 `
  --set-env-vars=GCP_PROJECT_ID=$P

# long-youtube-uploader
gcloud.cmd --project=$P run jobs create long-youtube-uploader --region=$R --image=$IMG/long-youtube-uploader:prod `
  --set-env-vars=GCP_PROJECT_ID=$P,YOUTUBE_PRIVACY_STATUS=private,YOUTUBE_CATEGORY_ID=24,YOUTUBE_MADE_FOR_KIDS=false,YOUTUBE_NOTIFY_SUBSCRIBERS=false,YOUTUBE_UPLOAD_CHUNK_BYTES=67108864,YOUTUBE_CHUNK_TIMEOUT_SECONDS=900,YOUTUBE_UPLOAD_MAX_RETRIES=8 `
  --update-secrets=YOUTUBE_CLIENT_ID=youtube-client-id:latest,YOUTUBE_CLIENT_SECRET=youtube-client-secret:latest,YOUTUBE_REFRESH_TOKEN=youtube-refresh-token:latest

# long-coordinator (giữ LONG_SOURCE_CACHE_VERSION=v4 để hit cache đã copy)
gcloud.cmd --project=$P run jobs create long-coordinator --region=$R --image=$IMG/long-coordinator:prod `
  --set-env-vars=GCP_PROJECT_ID=$P,RUN_REGION=$R,WORKER_JOB_NAME=long-worker,ASSEMBLER_JOB_NAME=long-assembler,YOUTUBE_UPLOADER_JOB_NAME=long-youtube-uploader,REVIEW_JOB_NAME=long-review,WORKER_FANOUT=2,WORKER_SOURCE_MAX_ATTEMPTS=2,LONG_SOURCE_CACHE_VERSION=v4,YOUTUBE_UPLOAD_ENABLED=true,LONG_REVIEW_MODE=false,VERTEX_REGION=global,GEMINI_AD_MODEL=gemini-2.5-pro

# long-job-runner (service). Sau khi deploy lấy URL -> set lại SERVICE_BASE_URL nếu dùng download/preview link.
gcloud.cmd --project=$P run deploy long-job-runner --region=$R --image=$IMG/long-runner:prod `
  --set-env-vars=GCP_PROJECT_ID=$P,RUN_REGION=$R,JOB_NAME=long-coordinator,OUTPUT_ROOT=$MEDIA,SCRATCH_ROOT=$SCRATCH,API_KEY=<RUNNER_API_KEY>,DOWNLOAD_SECRET=<DOWNLOAD_SECRET>
```

> Đây là bộ env tối thiểu bám theo `docs/DEPLOY.md`; **đối chiếu lại DEPLOY.md** phòng khi có env mới
> phát sinh. Nếu prod cũ đang set thêm gì (vd override chunk ở coordinator) thì bê theo.

---

## 6. Nối lại n8n

Trong workflow n8n (`docs/Douyin bot - long direct YouTube upload.json`), sửa các node HTTP trỏ về
runner mới:
- `Gọi Cloud Run Long` / `Gọi Cloud Run Cut` / `Gọi Cloud Run Upload` (+ `/review` nếu có): đổi
  base URL sang URL service `long-job-runner` mới, và `X-API-Key` = `<RUNNER_API_KEY>` mới.
- `callback_url` trong body `/run`: trỏ về webhook n8n như cũ (n8n có thể giữ nguyên nếu không đổi).

---

## 7. Kiểm chứng cache đã sang được

Gửi 1 `/long` với **1 URL chắc chắn đã cache ở env cũ** (vd 1 link trong batch cũ), `callback_enabled=false`:

```powershell
$u="https://<runner-moi>"; $k="<RUNNER_API_KEY>"
$p="$env:TEMP\smoke.json"
[System.IO.File]::WriteAllText($p,'{"id":"cache-check","videos":[{"douyin_url":"https://v.douyin.com/<link-da-cache>/"}],"callback_enabled":false,"youtube_upload_enabled":false}')
curl.exe -sS -X POST "$u/run" -H "Content-Type: application/json" -H "X-API-Key: $k" --data "@$p"
```

Đọc `status.json` của batch đó — nếu cache sang đúng thì source hiện **`status: "cached"`** trong
`cached_sources` và **worker không chạy lại** (không tốn Gemini/TTS/render):

```powershell
gcloud.cmd --project=<NEW_PROJECT> storage cat gs://<NEW_MEDIA>/long/cache-check/status.json
```

Kỳ vọng: `cached_sources` chứa link đó, `completed_sources` rỗng cho link đó → **cache hit OK**.

---

## 8. Ghi chú
- Env cũ vẫn nguyên vẹn — nếu env mới trục trặc, trỏ n8n về runner cũ là chạy lại được (rollback dễ).
- **Tuyệt đối không bump `LONG_SOURCE_CACHE_VERSION`** trong lúc migrate nếu muốn tái dùng cache đã copy.
- Nhớ kiểm **quota Gemini** ở project mới (mặc định thường thấp) — batch nhiều link dễ 429; xin tăng
  quota `gemini-2.5-pro` (Vertex `global`) trước khi chạy tải nặng.
- Việc copy cache là ops thuần (copy blob GCS) — **không cần sửa code** `douyin_shorts`.
