<#
.SYNOPSIS
    Full deploy script for SQS + Lambda + ECR.

.DESCRIPTION
    1. Reads Terraform outputs to get ECR repository URL and Lambda names
    2. Builds the Content Extractor Docker image
    3. Pushes the image to private ECR
    4. Runs terraform apply (creates/updates all resources)
    5. Updates the Content Extractor Lambda with the new image
    6. Registers the Telegram webhook with the API Gateway URL

.PARAMETER TerraformDir
    Path to the Terraform directory (default: ./infra/terraform)

.PARAMETER WhisperModelSize
    Whisper model to bake into the Docker image: tiny | base | small (default: base)

.PARAMETER SkipDockerBuild
    Skip the Docker build/push step (useful for fast infra-only deploys)

.PARAMETER RegisterWebhook
    After deploy, register the Telegram webhook automatically (requires TELEGRAM_BOT_TOKEN in .env)

.EXAMPLE
    # Full deploy
    .\scripts\deploy.ps1

    # Skip Docker build (e.g. only Lambda/SQS config changed)
    .\scripts\deploy.ps1 -SkipDockerBuild

    # Deploy with webhook registration
    .\scripts\deploy.ps1 -RegisterWebhook
#>

param(
    [string]$TerraformDir     = "$PSScriptRoot\..\infra\terraform",
    [string]$WhisperModelSize = "base",
    [switch]$SkipDockerBuild,
    [switch]$RegisterWebhook
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path "$PSScriptRoot\.."

# --- Helpers ------------------------------------------------------------------

function Write-Step([string]$msg) {
    Write-Host "`n--- $msg ---" -ForegroundColor Cyan
}

function Write-Success([string]$msg) {
    Write-Host "[OK] $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "[WARN] $msg" -ForegroundColor Yellow
}

function Require-Command([string]$cmd) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "Required command '$cmd' not found. Please install it and add to PATH."
    }
}

function Get-TfOutput([string]$key) {
    $val = terraform output -raw $key 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($val)) {
        return $null
    }
    return $val
}

# --- Pre-flight checks --------------------------------------------------------

Write-Step "Pre-flight checks"
Require-Command "terraform"
Require-Command "aws"
if (-not $SkipDockerBuild) {
    Require-Command "docker"
}

# Load .env file for TELEGRAM_BOT_TOKEN etc.
$EnvFile = "$ProjectRoot\.env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$") {
            $name  = $Matches[1].Trim()
            $value = $Matches[2].Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Write-Success "Loaded .env"
}

# Verify AWS credentials
Write-Step "Verifying AWS credentials"
$Identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if (-not $Identity.Account) { throw "AWS credentials not configured. Run 'aws configure'." }
$AwsAccountId = $Identity.Account
$AwsRegion    = $env:AWS_REGION
if ([string]::IsNullOrEmpty($AwsRegion)) {
    $AwsRegion = (aws configure get region 2>$null) -replace "`n",""
}
Write-Success "AWS Account: $AwsAccountId  Region: $AwsRegion"

# --- Step 1: terraform init (idempotent) --------------------------------------

Write-Step "Terraform init"
Push-Location $TerraformDir
terraform init -upgrade -input=false
Write-Success "Terraform initialised"

# --- Step 2: Create ECR repo first (needed before Docker push) ----------------

if (-not $SkipDockerBuild) {
    Write-Step "Creating ECR repository (if not exists)"
    terraform apply "-target=aws_ecr_repository.content_extractor" -auto-approve -input=false

    # Construct the ECR URL deterministically from account+region instead of
    # reading from `terraform output` - that output can return stale URLs if
    # state has any leftover resources from previous experiments.
    $EcrRepoName = "insta-agent-dev-content-extractor"
    $EcrRepoUrl  = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com/$EcrRepoName"
    Write-Success "ECR repo: $EcrRepoUrl"

    # --- Step 3: Build Docker image -------------------------------------------
    Write-Step "Building Content Extractor Docker image"
    $ImageTag = "$EcrRepoUrl`:latest"
    Pop-Location
    Push-Location $ProjectRoot

    # Lambda requires Docker schema v2 manifests, NOT OCI format.
    # Modern Docker Buildx defaults to OCI + provenance attestations which
    # Lambda rejects with "image manifest media type is not supported".
    # Flags below force the legacy schema-v2 single-platform amd64 build.
    docker build `
        --file lambdas/content_extractor/Dockerfile `
        --build-arg WHISPER_MODEL_SIZE=$WhisperModelSize `
        --platform linux/amd64 `
        --provenance=false `
        --sbom=false `
        --tag $ImageTag `
        .

    if ($LASTEXITCODE -ne 0) { throw "Docker build failed." }
    Write-Success "Docker image built: $ImageTag"

    # --- Step 4: Login and push image to private ECR --------------------------
    # Same-region pulls from Lambda are free; only storage is billed (~$0.10/GB/month).
    # Docker Desktop's Windows credential helper rejects long ECR tokens via stdin.
    # Fix: capture the token first, then pass via --password to avoid pipe encoding issues.
    Write-Step "Pushing image to ECR"
    $EcrToken = (aws ecr get-login-password --region $AwsRegion).Trim()
    if (-not $EcrToken) { throw "Failed to get ECR auth token." }

    $AuthValue    = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$EcrToken"))
    $EcrHost      = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com"
    $TmpConfigDir = "$env:TEMP\docker-ecr-push"
    if (-not (Test-Path $TmpConfigDir)) { New-Item -ItemType Directory -Path $TmpConfigDir | Out-Null }
    $TmpConfig = '{"auths":{"' + $EcrHost + '":{"auth":"' + $AuthValue + '"}}}'
    [System.IO.File]::WriteAllText("$TmpConfigDir\config.json", $TmpConfig, [System.Text.UTF8Encoding]::new($false))
    Write-Success "ECR credentials written to temp Docker config"

    docker --config $TmpConfigDir push $ImageTag
    if ($LASTEXITCODE -ne 0) { throw "Docker push failed." }
    Write-Success "Image pushed to ECR"

    # Update terraform.tfvars with the new image URI
    Push-Location $TerraformDir
    $TfVarsPath = "terraform.tfvars"
    $TfVarsContent = Get-Content $TfVarsPath -Raw
    if ($TfVarsContent -match 'extractor_image_uri\s*=\s*"[^"]*"') {
        $TfVarsContent = $TfVarsContent -replace 'extractor_image_uri\s*=\s*"[^"]*"', "extractor_image_uri = `"$ImageTag`""
    } else {
        $TfVarsContent += "`nextractor_image_uri = `"$ImageTag`""
    }
    Set-Content $TfVarsPath $TfVarsContent
    Write-Success "Updated terraform.tfvars with image URI: $ImageTag"
} else {
    Push-Location $TerraformDir
    Write-Warn "Skipping Docker build (--SkipDockerBuild flag set)"
}

# --- Step 5: Full terraform apply ---------------------------------------------

Write-Step "Running terraform apply"
terraform apply -auto-approve -input=false
if ($LASTEXITCODE -ne 0) { throw "terraform apply failed." }
Write-Success "Terraform apply complete"

# --- Step 6: Force Lambda code update (content extractor) --------------------

if (-not $SkipDockerBuild) {
    Write-Step "Updating Content Extractor Lambda with new image"
    $ExtractorFnName = Get-TfOutput "content_extractor_function_name"
    $EcrRepoName     = "insta-agent-dev-content-extractor"
    $EcrRepoUrl      = "$AwsAccountId.dkr.ecr.$AwsRegion.amazonaws.com/$EcrRepoName"
    $ImageTag        = "$EcrRepoUrl`:latest"

    if ($ExtractorFnName) {
        aws lambda update-function-code `
            --function-name $ExtractorFnName `
            --image-uri $ImageTag `
            --region $AwsRegion `
            --output json | Out-Null
        Write-Success "Lambda $ExtractorFnName updated"

        # Wait for Lambda to finish updating (image pull + optimization).
        # Without this, the next step or first real invocation may fail with
        # ResourceConflictException ("update in progress").
        Write-Step "Waiting for Lambda update to complete (~30-60s)"
        aws lambda wait function-updated `
            --function-name $ExtractorFnName `
            --region $AwsRegion
        Write-Success "Lambda is ready"

        # Warm-up invocation: triggers the first cold start NOW (with no real
        # payload) so that when an actual Telegram message arrives, the image
        # is already cached and the user gets a fast response.
        Write-Step "Warming up Lambda (pre-cache the image)"
        $warmPayload = '{"Records":[]}'
        $warmPayloadFile = "$env:TEMP\warm-payload.json"
        [System.IO.File]::WriteAllText($warmPayloadFile, $warmPayload, [System.Text.UTF8Encoding]::new($false))
        aws lambda invoke `
            --function-name $ExtractorFnName `
            --payload "fileb://$warmPayloadFile" `
            --region $AwsRegion `
            --cli-binary-format raw-in-base64-out `
            "$env:TEMP\warm-output.json" | Out-Null
        Write-Success "Lambda warmed up - bot is ready to use"
    }
}

# --- Step 6b: Verify AI Analyzer ----------------------------
#
# The AI Analyzer is a plain zip Lambda — Terraform applies new code
# automatically when handler.py / schema.py / prompts.py / groq_client.py
# change (archive_file produces a fresh hash). All we do here is print its
# status and warm it up if deployed.

Write-Step "Checking AI Analyzer Lambda"
$AnalyzerFnName = Get-TfOutput "ai_analyzer_function_name"

if ([string]::IsNullOrEmpty($AnalyzerFnName)) {
    Write-Warn "AI Analyzer not deployed. Set 'groq_api_key' in terraform.tfvars to enable it."
} else {
    Write-Success "AI Analyzer deployed: $AnalyzerFnName"

    # Warm-up: pre-pay the import cost so first real message is fast.
    Write-Step "Warming up AI Analyzer Lambda"
    $warmPayload = '{"Records":[]}'
    $warmPayloadFile = "$env:TEMP\warm-analyzer-payload.json"
    [System.IO.File]::WriteAllText($warmPayloadFile, $warmPayload, [System.Text.UTF8Encoding]::new($false))
    aws lambda invoke `
        --function-name $AnalyzerFnName `
        --payload "fileb://$warmPayloadFile" `
        --region $AwsRegion `
        --cli-binary-format raw-in-base64-out `
        "$env:TEMP\warm-analyzer-output.json" | Out-Null
    Write-Success "AI Analyzer warmed up"
}

# --- Step 7: Register Telegram webhook ---------------------------------------

if ($RegisterWebhook) {
    Write-Step "Registering Telegram webhook"
    $WebhookUrl = Get-TfOutput "telegram_webhook_url"
    $BotToken   = $env:TELEGRAM_BOT_TOKEN

    if (-not $WebhookUrl) { Write-Warn "Could not get webhook URL - skipping webhook registration" }
    elseif (-not $BotToken) { Write-Warn "TELEGRAM_BOT_TOKEN not set - skipping webhook registration" }
    else {
        $TelegramUrl = "https://api.telegram.org/bot$BotToken/setWebhook"
        $Body = @{ url = $WebhookUrl } | ConvertTo-Json
        $Response = Invoke-RestMethod -Uri $TelegramUrl -Method Post -Body $Body -ContentType "application/json"
        if ($Response.ok) {
            Write-Success "Telegram webhook registered: $WebhookUrl"
        } else {
            Write-Warn "Webhook registration failed: $($Response.description)"
        }
    }
}

# --- Summary ------------------------------------------------------------------

Pop-Location

Write-Step "Deploy complete"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Register Telegram webhook (if not done):"
$WebhookUrl = & { Push-Location $TerraformDir; $u = terraform output -raw telegram_webhook_url 2>$null; Pop-Location; $u }
Write-Host "       POST https://api.telegram.org/bot<TOKEN>/setWebhook" -ForegroundColor Gray
Write-Host "       Body: { `"url`": `"$WebhookUrl`" }" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Test by sending an Instagram URL to your Telegram bot"
Write-Host ""
Write-Host "  3. Monitor Lambda logs:"
Write-Host "       aws logs tail /aws/lambda/insta-agent-dev-content-extractor --follow" -ForegroundColor Gray
Write-Host "       aws logs tail /aws/lambda/insta-agent-dev-ai-analyzer       --follow" -ForegroundColor Gray
Write-Host ""
