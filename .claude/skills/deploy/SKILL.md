---
description: Deploy the SG360 BOL Reconciliation app to AWS (Lambda backend + S3/CloudFront frontend) via deploy.ps1, with pre-flight checks, a human-reviewed terraform apply gate, and post-deploy health/data verification.
---

# /deploy — SG360 BOL AWS Deploy & Verify

## Architecture note — read this first

There is **no EC2 server or long-running process to "boot up" or "kill."** The app is 100% serverless:

- **Backend** — Lambda (container image, pulled from ECR by digest) behind an API Gateway HTTP API. "Redeploying" means building a new Docker image, pushing it to ECR, and pointing the Lambda function at the new image via Terraform. AWS replaces running execution environments automatically on the next invoke — there's nothing to manually stop/start.
- **Database** — Aurora Serverless v2 Postgres, VPC-private. Only reachable from Lambda's security group — not from a dev machine directly.
- **Frontend** — S3 static bucket + CloudFront. Redeploying means build → `aws s3 sync --delete` → CloudFront invalidation.

`deploy.ps1` (repo root) already encodes the real mechanics for both halves. This skill wraps it with pre-flight checks, the human-approval gate for `terraform apply`, and post-deploy verification — it does not reimplement the deploy logic itself.

## What this skill does

1. Pre-flight checks (branch, working tree, Docker, AWS credentials)
2. Backend deploy: build → push to ECR → bump `lambda_image_tag` → `terraform plan` (via `deploy.ps1 -Backend`)
3. **Stops and shows you the plan** — asks for explicit go-ahead before running `terraform apply`
4. Frontend deploy: build → S3 sync → CloudFront invalidation (via `deploy.ps1 -Frontend`, fully automatic, no gate)
5. Verifies the live deployment: health check, record counts, and (optionally) a real data pull against on-prem sources
6. Reports errors, warnings, and anything needing attention

---

## Deploy steps

### Step 1 — Pre-flight checks

```powershell
cd C:\nikhilm\excel-prophesy-BOL-automation

# Branch check
$branch = git rev-parse --abbrev-ref HEAD
if ($branch -ne "main") { Write-Host "ERROR: not on main (currently on $branch) — stop and confirm with the user" -ForegroundColor Red }

# Working tree check — dirty terraform/main files change what terraform plan/apply will do
$dirty = git status --porcelain
$dirtyTf = $dirty | Where-Object { $_ -match 'terraform/main/' }
if ($dirtyTf) {
    Write-Host "Uncommitted changes inside terraform/main — this WILL be picked up by terraform plan/apply regardless of git status:" -ForegroundColor Yellow
    git diff -- terraform/main
    Write-Host "Stop and ask the user how to handle this (commit / stash / revert) before continuing." -ForegroundColor Yellow
}
if ($dirty -and -not $dirtyTf) { Write-Host "Note: other uncommitted changes exist (won't affect infra):"; git status --porcelain }

# Docker daemon
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Docker daemon not running" -ForegroundColor Red }

# AWS credentials
aws sts get-caller-identity 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: AWS CLI not authenticated" -ForegroundColor Red }
```

Do not proceed past a `terraform/main` dirty-tree finding without the user explicitly telling you how to handle it — it directly changes what gets applied to live infrastructure.

### Step 2 — Backend deploy (build, push, plan)

```powershell
cd C:\nikhilm\excel-prophesy-BOL-automation
.\deploy.ps1 -Backend
```

This builds `sg360-bol-api:live-<timestamp>`, pushes it to ECR, bumps `lambda_image_tag` in `terraform/main/terraform.tfvars`, and runs `terraform plan -out=tfplan_<tag>` — then stops by design (see the script's own header comment: infra changes always get a human look before they land).

**Watch for:**
- `Docker build failed.` — check `docker info`, check Dockerfile syntax
- `Could not read ecr_repository_url from Terraform output.` — terraform state missing/stale in `terraform/main`; run `terraform output` there manually to debug
- `ECR login failed.` — AWS credentials expired/wrong region (script uses `us-east-1`)
- `Terraform plan failed.` — read the plan error output directly; often a stale/locked state file (`terraform/main/terraform.tfstate`)

### Step 3 — Human review gate (REQUIRED — do not skip)

Print the plan output to the user (what resources it will add/change/destroy — for a normal image-tag bump this should be a 1-resource in-place update to `aws_lambda_function.app`; anything touching WAF, IAM, security groups, or Aurora deserves extra scrutiny).

**Ask the user explicitly, in this session, before running apply.** This is not a standing permission — always ask, every deploy. Only after they say yes:

```powershell
cd C:\nikhilm\excel-prophesy-BOL-automation\terraform\main
terraform apply "tfplan_<tag>"    # exact filename printed by deploy.ps1 in Step 2
```

### Step 4 — Frontend deploy (fully automatic)

```powershell
cd C:\nikhilm\excel-prophesy-BOL-automation
.\deploy.ps1 -Frontend
```

Builds the Vite app, syncs `frontend/dist` to the `frontend_bucket_name` S3 bucket with `--delete`, and invalidates CloudFront (`/*`). No approval gate — matches the script's own design (static assets, trivially re-deployable).

### Step 5 — Post-deploy verification

```powershell
$base = "https://d31fux83mramn1.cloudfront.net"   # or terraform output -raw cloudfront_url

$health = Invoke-WebRequest "$base/api/health" -UseBasicParsing | ConvertFrom-Json
# Expect: db_online = true, mock_mode = false

$bols = Invoke-WebRequest "$base/api/bols" -UseBasicParsing | ConvertFrom-Json
$approved = Invoke-WebRequest "$base/api/bols/approved" -UseBasicParsing | ConvertFrom-Json
```

**Interpret the health response:**
- `mock_mode: false` and `db_online: true` → Aurora reachable from Lambda, deploy succeeded structurally
- `db_online: false` → Aurora unreachable — check `lambda_sql_security_group.tf` / VPC config, check the static-IP DNS workaround in `backend/config.py` is still valid
- Any 5xx / timeout → check CloudWatch Logs for the `sg360-bol-api` Lambda function; likely a runtime error in the new image

**To confirm the deployed Lambda can still reach on-prem sources (not just Aurora)** — this is the real proof that "servers are still connected and data is being pulled":

```powershell
Invoke-WebRequest "$base/api/admin/pull" -Method POST -UseBasicParsing | ConvertFrom-Json
```

A successful response (with a manifest/record count, not an error) confirms Lambda→AWP-SQL-PROD/VisualMail connectivity survived the redeploy. This actually pulls live data — mention to the user that it will run before triggering it, since it's a real operation against production sources, not a read-only check.

### Step 6 — Report

Summarize: image tag deployed, whether apply ran (and what changed), frontend sync result, health check result, whether the data-pull check was run and what it returned. Flag anything unexpected using the error table below.

---

## Common errors and fixes

| Error | Cause | Fix |
|---|---|---|
| `Port already in use` / N/A | Not applicable — no local server for this skill | — |
| `Could not read ecr_repository_url` | `terraform/main` state missing or not initialized | `terraform init` in `terraform/main`, or check `terraform.tfstate` exists |
| ECR login 400/403 | Expired AWS SSO session or wrong region | Re-authenticate AWS CLI; script assumes `us-east-1` |
| `terraform plan` shows unexpected WAF/IAM/security-group changes | Uncommitted or unexpected `.tf` edits picked up from the working tree | Stop, show the user `git diff -- terraform/main`, get explicit direction |
| `db_online: false` after deploy | Aurora unreachable from Lambda, or static-IP DNS workaround in `backend/config.py` stale | Check VPC/security-group config; re-resolve `sg360-bol-aurora...rds.amazonaws.com` and the S3 endpoint IP if AWS's infra shifted |
| `POST /api/admin/pull` fails or times out | On-prem VPN/DNS path broken, or AWP-SQL-PROD credentials issue | Check CloudWatch Logs for the Lambda; this is the same class of issue documented in `documentation/Developmental Documentation.md` (2026-07-09 entry) |
| Frontend loads but shows stale content | CloudFront cache not invalidated, or invalidation still in progress | Re-run invalidation; check `aws cloudfront get-invalidation` status |

---

## Environment overview

- **AWS account:** `610614956027`, region `us-east-1`
- **Backend:** Lambda `sg360-bol-api`, container image via ECR repo `sg360-bol-app`, API Gateway HTTP API
- **Frontend:** S3 bucket `sg360-bol-frontend` + CloudFront (`cloudfront_url` terraform output)
- **Database:** Aurora Serverless v2 Postgres, cluster `sg360-bol-aurora`, VPC-private
- **Invoice PDFs:** S3 bucket `sg360-bol-invoices`, private, Lambda role has `PutObject`/`GetObject` only
- **Terraform:** `terraform/main` — local state (not yet migrated to the S3 backend `terraform/bootstrap` provisioned); `terraform.tfvars` is tracked in git and holds the live `lambda_image_tag`
- **Config/secrets:** Lambda reads `AWS_SECRET_NAME=sg360-bol-live-credentials` from Secrets Manager instead of `.env` (see `backend/config.py`)
