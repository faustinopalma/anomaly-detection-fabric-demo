<#
.SYNOPSIS
    Deploy the simulator control panel (webapp/) to an Azure Static Web App.

.DESCRIPTION
    Local-only deploy helper. It:
      1. Ensures you are logged in to the right tenant (device code, idempotent)
      2. Creates / reuses a Free-SKU Static Web App via infra/swa.bicep
      3. Publishes the static files in webapp/ using the SWA CLI (swa deploy)

    It does NOT touch the live Container App. Enabling the control API on
    `ca-simulator` (external ingress + SIM_CONTROL_* env vars) is a manual
    step — see infra/swa.bicep and webapp/README.md — so the running demo is
    never changed unattended.

.EXAMPLE
    pwsh ./webapp/deploy.ps1

.EXAMPLE
    pwsh ./webapp/deploy.ps1 -Name swa-anomaly-sim -Location eastus2
#>
[CmdletBinding()]
param(
    [string]$RgName,
    [string]$Name     = "swa-anomaly-sim",
    [string]$Location  = "westeurope"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 0. Load .env from repo root (reuse the same loader convention)
# ---------------------------------------------------------------------------
$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$envFile  = Join-Path $repoRoot ".env"
if (-not (Test-Path $envFile)) { throw "Cannot find .env at $envFile" }

Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $k = $line.Substring(0, $idx).Trim()
    $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$k" -Value $v
}

if (-not $env:FABRIC_TENANT_ID)     { throw "FABRIC_TENANT_ID is missing in .env" }
if (-not $env:FABRIC_CAPACITY_NAME) { throw "FABRIC_CAPACITY_NAME is missing in .env" }

# ---------------------------------------------------------------------------
# 1. Login (device code, idempotent)
# ---------------------------------------------------------------------------
$ctx = az account show 2>$null | ConvertFrom-Json
if (-not $ctx -or $ctx.tenantId -ne $env:FABRIC_TENANT_ID) {
    Write-Host "[swa] az login --use-device-code --tenant $($env:FABRIC_TENANT_ID)" -ForegroundColor Cyan
    az login --use-device-code --tenant $env:FABRIC_TENANT_ID | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
} else {
    Write-Host "[swa] reusing existing az session ($($ctx.user.name))" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 2. Discover the resource group from the Fabric capacity (unless overridden)
# ---------------------------------------------------------------------------
if (-not $RgName) {
    Write-Host "[swa] discovering resource group from Fabric capacity '$($env:FABRIC_CAPACITY_NAME)'" -ForegroundColor Cyan
    $RgName = az resource list `
        --name $env:FABRIC_CAPACITY_NAME `
        --resource-type "Microsoft.Fabric/capacities" `
        --query "[0].resourceGroup" -o tsv
    if (-not $RgName) { throw "Could not resolve resource group for capacity $($env:FABRIC_CAPACITY_NAME). Pass -RgName explicitly." }
}
Write-Host "[swa] resource group: $RgName" -ForegroundColor Green

az extension add --name staticwebapp --upgrade --only-show-errors 2>$null | Out-Null

# ---------------------------------------------------------------------------
# 3. Create / reuse the Static Web App (Free SKU) via Bicep
# ---------------------------------------------------------------------------
Write-Host "[swa] deploying infra/swa.bicep ($Name in $Location)" -ForegroundColor Cyan
az deployment group create `
    --resource-group $RgName `
    --template-file (Join-Path $PSScriptRoot "../infra/swa.bicep") `
    --parameters name=$Name location=$Location `
    --only-show-errors | Out-Null
if ($LASTEXITCODE -ne 0) { throw "swa.bicep deployment failed" }

$hostname = az staticwebapp show -n $Name -g $RgName --query "defaultHostname" -o tsv
Write-Host "[swa] static site hostname: https://$hostname" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4. Publish the static files with the SWA CLI
# ---------------------------------------------------------------------------
$token = az staticwebapp secrets list -n $Name -g $RgName --query "properties.apiKey" -o tsv
if (-not $token) { throw "Could not read SWA deployment token" }

if (-not (Get-Command swa -ErrorAction SilentlyContinue)) {
    Write-Host "[swa] installing the SWA CLI (npm i -g @azure/static-web-apps-cli)" -ForegroundColor Cyan
    npm install -g @azure/static-web-apps-cli
    if ($LASTEXITCODE -ne 0) { throw "Failed to install @azure/static-web-apps-cli (is Node.js installed?)" }
}

Write-Host "[swa] publishing webapp/ to the static site" -ForegroundColor Cyan
swa deploy $PSScriptRoot --deployment-token $token --env production
if ($LASTEXITCODE -ne 0) { throw "swa deploy failed" }

Write-Host ""
Write-Host "[swa] done. Open: https://$hostname" -ForegroundColor Green
Write-Host "[swa] In the page, set the API base URL to the Container App control endpoint" -ForegroundColor Yellow
Write-Host "      and the API key to SIM_CONTROL_API_KEY." -ForegroundColor Yellow
Write-Host "[swa] Reminder (manual, not done here): enable external ingress on ca-simulator" -ForegroundColor Yellow
Write-Host "      and set SIM_CONTROL_ENABLED=1 / SIM_CONTROL_API_KEY / SIM_CONTROL_CORS_ORIGINS." -ForegroundColor Yellow
