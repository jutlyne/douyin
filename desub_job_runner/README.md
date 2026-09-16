# Cover Visub runner

Independent Cloud Run service for the CPU-only Cover Visub flow. It accepts one
Douyin URL and asynchronously starts `cover-visub-lab`.

```http
POST /run
X-API-Key: <secret>
Content-Type: application/json

{"douyin_url":"https://v.douyin.com/example/","force_refresh":false}
```

The response is HTTP 202 and contains `desub_id`, `status_url`, `status_uri`,
`output_uri`, `report_uri`, and an HMAC `download_url`. Poll `GET /status` with
the same API key until `status=completed`.

Deployed service:

```text
https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app
```

Example from PowerShell (the key remains in Secret Manager):

```powershell
$key = (gcloud secrets versions access latest `
  --secret=desub-runner-api-key `
  --project=YOUR_GCP_PROJECT).Trim()
$headers = @{ 'X-API-Key' = $key }
$body = @{ douyin_url='https://v.douyin.com/example/' } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post `
  -Uri 'https://desub-job-runner-YOUR_RUN_HASH-as.a.run.app/run' `
  -Headers $headers -ContentType 'application/json' -Body $body
$run
Invoke-RestMethod -Uri $run.status_url -Headers $headers
```

Deployment uses Cloud Run automatic scaling with minimum 0 and maximum 20
instances. `cover-visub-lab` remains a separate CPU-only Cloud Run Job.

The service is isolated from `job_runner`, `long_job_runner`,
`container_short`, and `container_long`.
