# DESUB Cloud Run jobs.run dry-run record

Evidence date: 2026-07-23
Decision status: **BLOCKED / fail-closed — launch correlation is not proven.**

## What the API supports

Cloud Run v2 `projects.locations.jobs.run` accepts `validateOnly` and execution
`overrides`, including `containerOverrides[].env`; use of overrides requires
`run.jobs.runWithOverrides`. [Official `jobs.run` reference](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run)

An Execution exposes an output-only `template`, which is the planned evidence
location for the immutable launch environment. [Official Execution
reference](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions)

`validateOnly: true` is suitable for validating request shape and caller IAM.
It does **not** establish, by itself, that a persisted Execution exists whose
template contains the supplied launch token. Therefore it cannot satisfy the
plan's positive-correspondence requirement.

## Sanitized validation request template

This is a data template, not an executable command. Replace only after an
authorized Gate-A test has selected a dedicated controlled job.

```json
{
  "validateOnly": true,
  "overrides": {
    "taskCount": 1,
    "containerOverrides": [
      {
        "env": [
          {"name": "DESUB_LAUNCH_TOKEN", "value": "[random-128-bit-token]"},
          {"name": "DESUB_JOB_ID", "value": "[opaque-job-id]"},
          {"name": "DESUB_ATTEMPT_ID", "value": "[opaque-attempt-id]"},
          {"name": "DESUB_ATTEMPT_SEQ", "value": "[monotonic-sequence]"}
        ]
      }
    ]
  }
}
```

The eventual request is `POST
https://run.googleapis.com/v2/projects/[project]/locations/[region]/jobs/[job]:run`.
The caller must hold the documented `run.jobs.runWithOverrides` permission.

## Mandatory evidence checklist

Before Gate A can clear this item, record all of the following for the target
project, region, and dedicated controlled job:

1. Read-only job description: exact job resource, image digest, revision/job
   generation, service account, and region. Do not rely on a default gcloud
   project, account, or region.
2. `validateOnly` request/response, sanitized and hashed, proving the body
   schema and caller IAM.
3. Explicit user authorization for one controlled no-op execution. It must use
   a job/image designed to produce no media and no external side effect.
4. Persisted operation name and execution name immediately after launch.
5. `GET` of that exact Execution, with a saved redacted response proving the
   exact launch token and all four `DESUB_*` environment values in
   `Execution.template`.
6. Negative checks: labels are not used for correlation; list results of zero
   are never treated as permission to relaunch; multiple exact-token matches
   fail closed.
7. Reconciler evidence for one/zero/multiple positive matches, including the
   plan's `30s -> 60s -> 2m` lookup backoff and 15-minute unresolved fence.

## Local audit observation

The local default gcloud profile resolved to a different project than the
repository's `develop` profile. The `develop` profile resolved to
`YOUR_GCP_PROJECT`, but this audit did not obtain a configured account or region.
All future approved commands must specify configuration, project, and region
explicitly. Current local runner code has a legacy Cover Visub `jobs.run`
caller; it is not evidence of the planned integrated DESUB job, launch-token
handling, or reconciler.

Until the checklist is complete, every ambiguous launch remains `unknown` and
must never be relaunched blindly or promoted to a user-visible result.
