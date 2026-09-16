# Deploy and rebuild guide

Project/region hiện tại:

```text
GCP project: YOUR_GCP_PROJECT
Region: asia-southeast1
Artifact Registry repo: us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin
Main branch: main
```

Các lệnh dưới đây dùng Windows PowerShell và `gcloud.cmd --configuration=develop`.

## 1. Kiểm tra trước deploy

```powershell
git status --short
python -m unittest tests.test_long_batch_checkpoint
```

Nếu có thay đổi liên quan short/long subtitles, chạy thêm test tương ứng trong `tests/`.

## 2. Build long prod images

Build đủ worker, assembler, coordinator, runner và uploader:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=container_long\cloudbuild.prod.yaml .
```

Image được push:

```text
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-worker:prod
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-assembler:prod
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-coordinator:prod
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-runner:prod
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-youtube-uploader:prod
us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-review:prod
```

## 3. Deploy long prod

### long-worker

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-worker `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-worker:prod `
  --set-env-vars=LONG_CHUNK_SECONDS=90,LONG_MIN_CHUNK_SECONDS=60,LONG_MAX_CHUNK_SECONDS=120,LONG_ANALYSIS_PADDING_SECONDS=3,LONG_VISUAL_REFRESH=auto,LONG_VIDEO_CRF=18,LONG_VIDEO_WIDTH=1920,LONG_VIDEO_HEIGHT=1080
```

### long-assembler

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-assembler `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-assembler:prod
```

### long-youtube-uploader

Secret names required:

```text
youtube-client-id
youtube-client-secret
youtube-refresh-token
```

Create/update secret versions manually if needed:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT secrets versions add youtube-client-id --data-file="$env:USERPROFILE\Downloads\youtube-client-id.txt"
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT secrets versions add youtube-client-secret --data-file="$env:USERPROFILE\Downloads\youtube-client-secret.txt"
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT secrets versions add youtube-refresh-token --data-file="$env:USERPROFILE\Downloads\youtube-refresh-token.txt"
```

Deploy uploader:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-youtube-uploader `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-youtube-uploader:prod `
  --set-env-vars=YOUTUBE_PRIVACY_STATUS=private,YOUTUBE_CATEGORY_ID=24,YOUTUBE_MADE_FOR_KIDS=false,YOUTUBE_NOTIFY_SUBSCRIBERS=false,YOUTUBE_UPLOAD_CHUNK_BYTES=67108864,YOUTUBE_CHUNK_TIMEOUT_SECONDS=900,YOUTUBE_UPLOAD_MAX_RETRIES=8 `
  --update-secrets=YOUTUBE_CLIENT_ID=youtube-client-id:latest,YOUTUBE_CLIENT_SECRET=youtube-client-secret:latest,YOUTUBE_REFRESH_TOKEN=youtube-refresh-token:latest
```

### long-review (ad-cut editor job)

New single job for the human-in-the-loop ad-review flow. It cuts confirmed ad
spans out of an assembled `final-long.mp4` (frame-accurate re-encode) and shifts
the SRT/chapters/metadata timeline. It touches large video files with ffmpeg, so
mirror the `long-assembler` resource config (inspect it first):

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs describe long-assembler --region=asia-southeast1
```

Create the job the first time (match assembler CPU/memory/timeout/service-account):

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs create long-review `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-review:prod `
  --cpu=<match-assembler> --memory=<match-assembler> --task-timeout=<match-assembler> `
  --service-account=<match-assembler> --max-retries=0
```

Later updates:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-review `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-review:prod
```

### long-coordinator

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-coordinator `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-coordinator:prod `
  --set-env-vars=WORKER_JOB_NAME=long-worker,ASSEMBLER_JOB_NAME=long-assembler,WORKER_FANOUT=2,WORKER_SOURCE_MAX_ATTEMPTS=2,LONG_SOURCE_CACHE_VERSION=v4,YOUTUBE_UPLOAD_ENABLED=true,YOUTUBE_UPLOADER_JOB_NAME=long-youtube-uploader,REVIEW_JOB_NAME=long-review,LONG_REVIEW_MODE=false,VERTEX_REGION=global,GEMINI_AD_MODEL=gemini-2.5-pro
```

Set `YOUTUBE_UPLOAD_ENABLED=false` if YouTube direct upload must be disabled and n8n should receive `download_url` instead.

**Review mode gating:** deploy with `LONG_REVIEW_MODE=false` first so the existing
`/long` flow keeps auto-uploading unchanged. To test the ad-review flow before n8n
is wired up, send `review_mode: true` in a single `/run` payload (opt-in per batch)
and drive `/cut` + `/upload` manually. Flip the global `LONG_REVIEW_MODE=true`
only **after** the n8n review workflow can handle `long.review.required` /
`long.cut.completed` and the `/cut`, `/upload` commands
(see `docs/TELEGRAM_REVIEW_FLOW.md`).

### long-job-runner service

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run services update long-job-runner `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/long-runner:prod `
  --set-env-vars=JOB_NAME=long-coordinator,OUTPUT_ROOT=gs://YOUR_GCP_PROJECT-media-sg/long,SCRATCH_ROOT=gs://YOUR_GCP_PROJECT-scratch-sg/long
```

Current service URL from Cloud Run:

```text
https://long-job-runner-YOUR_RUN_HASH-as.a.run.app
```

n8n may use either the current service URL or an existing Cloud Run URL alias; verify with service `status.url` after deploy.

## 4. Build/deploy short path

Short keeps the older n8n binary download + YouTube node path.

Build short job:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=container_short\cloudbuild.job.yaml .
```

Update `short-maker`:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update short-maker `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/short:job
```

Build short runner service:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=job_runner\cloudbuild.yaml .
```

Update `short-job-runner`:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run services update short-job-runner `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/job-runner:latest
```

## 5. n8n self-host deploy notes

Current n8n service:

```text
Service: n8n-selfhost
URL: https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app
Image: docker.io/n8nio/n8n:2.26.8
Cloud SQL: YOUR_GCP_PROJECT:asia-southeast1:n8n-postgres
Database: n8n
User: n8n
```

Important env currently used:

```text
DB_TYPE=postgresdb
DB_POSTGRESDB_HOST=/cloudsql/YOUR_GCP_PROJECT:asia-southeast1:n8n-postgres
DB_POSTGRESDB_DATABASE=n8n
DB_POSTGRESDB_USER=n8n
N8N_PROTOCOL=https
N8N_HOST=n8n-selfhost-YOUR_RUN_HASH-as.a.run.app
N8N_EDITOR_BASE_URL=https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app
WEBHOOK_URL=https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app
N8N_PROXY_HOPS=1
N8N_SECURE_COOKIE=true
EXECUTIONS_DATA_PRUNE=true
EXECUTIONS_DATA_MAX_AGE=336
N8N_RUNNERS_GRANT_TOKEN_TTL=300
N8N_PUSH_BACKEND=websocket
N8N_ENDPOINT_HEALTH=health
```

Secrets:

```text
n8n-db-password
n8n-encryption-key
```

Do not rotate `n8n-encryption-key` unless you also plan how to recover/recreate credentials.

Useful checks:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run services describe n8n-selfhost --region=asia-southeast1
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="n8n-selfhost"' --freshness=1h --limit=100 --order=desc
```

## 6. n8n manual recovery

`n8n-recovery` is a small Cloud Run service outside n8n. It exists for the case where Cloud Run reports `n8n-selfhost` as `Ready`, but n8n internally returns `503 Database is not ready` on `/rest/push`.

Current service URL:

```text
https://n8n-recovery-YOUR_RUN_HASH-as.a.run.app
```

Endpoints:

```text
/status?token=...   # check /rest/push without restarting
/restart?token=...  # force a new n8n-selfhost revision by updating N8N_MANUAL_RESTART_TS
```

Build the image:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT builds submit --config=n8n_watchdog\cloudbuild.service.prod.yaml .
```

Token secret:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT secrets create n8n-recovery-token --replication-policy=automatic
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT secrets versions add n8n-recovery-token --data-file="$env:USERPROFILE\Downloads\n8n-recovery-token.txt"
```

Deploy the service:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run deploy n8n-recovery `
  --region=asia-southeast1 `
  --image=us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin/n8n-recovery:prod `
  --service-account=YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com `
  --allow-unauthenticated `
  --set-env-vars=PROJECT_ID=YOUR_GCP_PROJECT,REGION=asia-southeast1,N8N_SERVICE=n8n-selfhost,N8N_PUSH_URL=https://n8n-selfhost-YOUR_RUN_HASH-as.a.run.app/rest/push?pushRef=manual-recovery,REQUEST_TIMEOUT_SECONDS=15 `
  --update-secrets=RECOVERY_TOKEN=n8n-recovery-token:latest `
  --min-instances=0 `
  --max-instances=1 `
  --memory=512Mi `
  --cpu=1
```

Manual checks:

```powershell
curl.exe -sS -i "https://n8n-recovery-YOUR_RUN_HASH-as.a.run.app/status?token=<TOKEN>"
curl.exe -sS -i "https://n8n-recovery-YOUR_RUN_HASH-as.a.run.app/restart?token=<TOKEN>"
```

Expected healthy status: `/status` returns `n8n_push_status=401`, because the n8n push endpoint requires an authenticated browser session. Call `/restart` only when n8n is stuck/offline.

There is no Cloud Scheduler for this service; recovery is manual.
## 7. Smoke test long after deploy

Call runner with one cached or known-good URL:

```powershell
$body = @{
  id = "deploy-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
  videos = @(@{ douyin_url = "https://v.douyin.com/rWN_en8Y76E/" })
  callback_enabled = $false
  youtube_upload_enabled = $false
} | ConvertTo-Json -Depth 5

curl.exe -X POST "https://long-job-runner-YOUR_RUN_HASH-as.a.run.app/run" `
  -H "Content-Type: application/json" `
  -H "X-API-Key: <LONG_RUNNER_API_KEY>" `
  --data $body
```

Then inspect:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs executions list --job=long-coordinator --region=asia-southeast1 --limit=5
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT storage ls --recursive gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT storage cat gs://YOUR_GCP_PROJECT-media-sg/long/<batch_id>/status.json
```

Expected for a successful direct YouTube upload:

```text
event=long.youtube.completed
ok=true
status=uploaded
youtube_url=https://www.youtube.com/watch?v=...
```

Expected for upload disabled:

```text
event=long.batch.completed
ok=true
status=completed
download_url=...
```

## 8. Rollback

Cloud Run jobs/services can be rolled back by updating image tags/digests.

List recent Artifact Registry images:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT artifacts docker images list us-docker.pkg.dev/YOUR_GCP_PROJECT/douyin --include-tags --limit=50
```

Update a job/service back to a known digest:

```powershell
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run jobs update long-coordinator --region=asia-southeast1 --image=<IMAGE_WITH_DIGEST>
gcloud.cmd --configuration=develop --project=YOUR_GCP_PROJECT run services update long-job-runner --region=asia-southeast1 --image=<IMAGE_WITH_DIGEST>
```

If a long full-video batch was already created, rerunning the same URL list without `--refresh` should reuse source cache and only redo assembly/upload.
