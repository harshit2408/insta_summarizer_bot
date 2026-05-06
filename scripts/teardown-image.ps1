<#
.SYNOPSIS
    Tear down the Content Extractor image from ECR to stop storage billing.

.DESCRIPTION
    Removes ALL images in the ECR repo for the Content Extractor.
    The Lambda function definition stays but cannot invoke until you
    re-run deploy.ps1 to push a fresh image.

    While torn down:
      - ECR storage cost: $0
      - Lambda cost:      $0 (no invocations possible)
      - Bot status:       OFFLINE (any incoming messages will go to DLQ)

    To use the bot again:
      .\scripts\deploy.ps1
      (rebuild + push, ~5-8 minutes thanks to local Docker cache)

.EXAMPLE
    .\scripts\teardown-image.ps1
#>

# Note: NOT using StrictMode or ErrorActionPreference='Stop' here, because
# AWS CLI writes informational messages to stderr which PowerShell would
# otherwise treat as fatal errors even when the command succeeded.

$RepoName = "insta-agent-dev-content-extractor"

$AwsRegion = $env:AWS_REGION
if ([string]::IsNullOrEmpty($AwsRegion)) {
    $AwsRegion = (aws configure get region 2>$null)
    if ($AwsRegion) { $AwsRegion = $AwsRegion.Trim() }
}
if ([string]::IsNullOrEmpty($AwsRegion)) {
    Write-Host "[ERROR] AWS region not configured. Run 'aws configure' first." -ForegroundColor Red
    exit 1
}

Write-Host "`n--- Listing images in $RepoName ---" -ForegroundColor Cyan

# Capture both stdout and exit code; suppress stderr noise
$listJson = & aws ecr list-images --repository-name $RepoName --region $AwsRegion --output json 2>$null
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host "[ERROR] Could not list images. Repo may not exist or you lack permissions." -ForegroundColor Red
    exit 1
}

$images = $listJson | ConvertFrom-Json
if (-not $images.imageIds -or $images.imageIds.Count -eq 0) {
    Write-Host "[OK] No images to delete - already torn down." -ForegroundColor Green
    exit 0
}

Write-Host "Found $($images.imageIds.Count) image(s) to delete." -ForegroundColor Yellow

Write-Host "`n--- Deleting images ---" -ForegroundColor Cyan
foreach ($img in $images.imageIds) {
    $idArg = "imageDigest=$($img.imageDigest)"
    & aws ecr batch-delete-image --repository-name $RepoName --region $AwsRegion --image-ids $idArg 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Deleted: $($img.imageDigest.Substring(0, 19))..." -ForegroundColor Gray
    } else {
        Write-Host "  Failed:  $($img.imageDigest.Substring(0, 19))..." -ForegroundColor Red
    }
}

Write-Host "`n[OK] Image deletion complete." -ForegroundColor Green
Write-Host "[OK] ECR storage billing has stopped." -ForegroundColor Green
Write-Host ""
Write-Host "Bot is now OFFLINE. To bring it back online:" -ForegroundColor Yellow
Write-Host "  .\scripts\deploy.ps1   (takes ~5-8 minutes with local Docker cache)" -ForegroundColor Gray
Write-Host ""
