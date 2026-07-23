# SG360 BOL Reconciliation - Deploy to AWS
# Run from the project root: .\deploy.ps1 [-Backend] [-Frontend]
# No switches = do both.
#
# Backend stops BEFORE `terraform apply` on purpose — review the printed plan,
# then run the apply command yourself from terraform/main. This mirrors the
# reviewed-apply process used to stand up this deployment in the first place;
# infra changes always get a human look before they land.
#
# Frontend runs fully automatically (build -> S3 sync -> CloudFront invalidation) —
# it only replaces static assets and is trivially re-deployable, so there's no
# review gate.

param(
    [switch]$Backend,
    [switch]$Frontend
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$tfDir = Join-Path $root "terraform\main"

if (-not $Backend -and -not $Frontend) {
    $Backend = $true
    $Frontend = $true
}

if ($Backend) {
    Write-Host "=== Backend deploy ===" -ForegroundColor Cyan

    $tag = "live-$(Get-Date -Format yyyyMMddHHmmss)"
    Write-Host "Building image sg360-bol-api:$tag ..." -ForegroundColor Green

    Set-Location $root
    docker build --provenance=false --platform linux/amd64 -t "sg360-bol-api:$tag" .
    # $LASTEXITCODE, not $?: docker/terraform write progress to stderr, and any
    # caller-side stream redirection turns those lines into ErrorRecords that
    # flip $? to false even when the command succeeded (exit code 0).
    if ($LASTEXITCODE -ne 0) { Write-Host "Docker build failed." -ForegroundColor Red; exit 1 }

    Set-Location $tfDir
    $ecrUrl = terraform output -raw ecr_repository_url
    if (-not $ecrUrl) { Write-Host "Could not read ecr_repository_url from Terraform output." -ForegroundColor Red; exit 1 }
    $registryHost = $ecrUrl.Split("/")[0]

    Write-Host "Logging in to ECR ($registryHost) ..." -ForegroundColor Green
    # --password-stdin breaks under Windows PowerShell's pipeline text handling
    # (confirmed: identical command works fine from Bash, fails from PS with a
    # 400 Bad Request). Using --password directly instead — acceptable here
    # since this is a short-lived, auto-scoped ECR push/pull token, not a
    # standing credential.
    $ecrPw = aws ecr get-login-password --region us-east-1
    docker login --username AWS --password $ecrPw $registryHost
    if ($LASTEXITCODE -ne 0) { Write-Host "ECR login failed." -ForegroundColor Red; exit 1 }

    docker tag "sg360-bol-api:$tag" "${ecrUrl}:$tag"
    docker push "${ecrUrl}:$tag"
    if ($LASTEXITCODE -ne 0) { Write-Host "Docker push failed." -ForegroundColor Red; exit 1 }

    Write-Host "Bumping lambda_image_tag to $tag in terraform.tfvars ..." -ForegroundColor Green
    $tfvarsPath = Join-Path $tfDir "terraform.tfvars"
    (Get-Content $tfvarsPath) -replace 'lambda_image_tag\s*=\s*".*"', "lambda_image_tag        = `"$tag`"" |
        Set-Content -Encoding utf8 $tfvarsPath

    $planFile = "tfplan_$tag"
    # NOTE: -out=$planFile (unquoted) silently passes the literal text "$planFile"
    # to terraform.exe instead of expanding the variable -- PowerShell doesn't
    # interpolate a bare $var joined to a flag with "=" when calling a native exe.
    # Quoting the whole argument forces proper expansion.
    terraform plan -out="$planFile"
    if ($LASTEXITCODE -ne 0) { Write-Host "Terraform plan failed." -ForegroundColor Red; exit 1 }

    Write-Host ""
    Write-Host "Plan saved: $tfDir\$planFile" -ForegroundColor Yellow
    Write-Host "Review it, then apply from $tfDir with:" -ForegroundColor Yellow
    Write-Host "    terraform apply `"$planFile`"" -ForegroundColor Yellow
    Write-Host ""

    Set-Location $root
}

if ($Frontend) {
    Write-Host "=== Frontend deploy ===" -ForegroundColor Cyan

    Set-Location (Join-Path $root "frontend")
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Host "Frontend build failed." -ForegroundColor Red; exit 1 }

    Set-Location $tfDir
    $bucket = terraform output -raw frontend_bucket_name
    $distId = terraform output -raw cloudfront_distribution_id
    if (-not $bucket -or -not $distId) { Write-Host "Could not read frontend outputs from Terraform." -ForegroundColor Red; exit 1 }

    # Two passes, in this order, so index.html never goes live pointing at an asset
    # that isn't in S3 yet:
    #   1. Everything except index.html gets long-lived immutable caching -- safe
    #      because Vite content-hashes these filenames (e.g. index-ByLbNsR8.js), so a
    #      new deploy always produces new filenames and never reuses an old cached URL.
    #   2. index.html itself gets no-cache metadata, so browsers always revalidate with
    #      the server before using it instead of serving a stale local copy from disk
    #      indefinitely under RFC 7234 heuristic caching (no Cache-Control header at
    #      all was previously sent for ANY file, including index.html -- confirmed via
    #      direct investigation as the actual cause of a "deployed site looks stale"
    #      report even though CloudFront's own edge cache already had the correct
    #      content and had already been invalidated).
    Write-Host "Syncing frontend/dist to s3://$bucket ..." -ForegroundColor Green
    aws s3 sync (Join-Path $root "frontend\dist") "s3://$bucket" --delete --cache-control "public, max-age=31536000, immutable" --exclude "index.html"
    if ($LASTEXITCODE -ne 0) { Write-Host "S3 sync failed." -ForegroundColor Red; exit 1 }

    aws s3 cp (Join-Path $root "frontend\dist\index.html") "s3://$bucket/index.html" --cache-control "no-cache, no-store, must-revalidate"
    if ($LASTEXITCODE -ne 0) { Write-Host "index.html upload failed." -ForegroundColor Red; exit 1 }

    Write-Host "Invalidating CloudFront distribution $distId ..." -ForegroundColor Green
    aws cloudfront create-invalidation --distribution-id $distId --paths "/*" | Out-Null

    $url = terraform output -raw cloudfront_url
    Write-Host ""
    Write-Host "Frontend deployed: $url" -ForegroundColor Cyan

    Set-Location $root
}
