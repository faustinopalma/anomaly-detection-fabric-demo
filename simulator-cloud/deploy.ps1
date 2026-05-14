<#
.SYNOPSIS
    Deploy the telemetry simulator to Azure Container Apps as an always-on
    single-replica workload. The Eventstream connection string is injected
    as an ACA secret (no Key Vault — overkill for one secret in a demo).

.DESCRIPTION
    Steps:
      1. az login --use-device-code (only if not already logged in)
      2. Discover the resource group from the Fabric capacity name in .env
      3. Create / reuse an Azure Container Registry in that RG
      4. az acr build (no local Docker required)
      5. Create / reuse a Container Apps environment
      6. Create / update the container app with min=max=1 replica

.EXAMPLE
    pwsh ./simulator-cloud/deploy.ps1

.EXAMPLE
    pwsh ./simulator-cloud/deploy.ps1 -Location northeurope -ImageTag v2
#>
[CmdletBinding()]
param(
    [string]$RgName,
    [string]$Location,   # default: same region as the Fabric capacity
    [string]$AcrName,
    [string]$EnvName  = "cae-anomalydet",
    [string]$AppName  = "ca-simulator",
    [string]$ImageTag = "latest",
    [int]   $Machines = 5,
    [double]$Rate     = 1.0,
    [double]$AnomalyProb = 0.0005
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 0. Load .env from repo root
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

if (-not $env:EVENTSTREAM_CONNECTION_STRING) {
    throw "EVENTSTREAM_CONNECTION_STRING is missing in .env"
}
if (-not $env:FABRIC_CAPACITY_NAME) {
    throw "FABRIC_CAPACITY_NAME is missing in .env"
}
if (-not $env:FABRIC_TENANT_ID) {
    throw "FABRIC_TENANT_ID is missing in .env"
}

# ---------------------------------------------------------------------------
# 1. Login (device code, idempotent)
# ---------------------------------------------------------------------------
$ctx = az account show 2>$null | ConvertFrom-Json
if (-not $ctx -or $ctx.tenantId -ne $env:FABRIC_TENANT_ID) {
    Write-Host "[deploy] az login --use-device-code --tenant $($env:FABRIC_TENANT_ID)" -ForegroundColor Cyan
    az login --use-device-code --tenant $env:FABRIC_TENANT_ID | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
} else {
    Write-Host "[deploy] reusing existing az session ($($ctx.user.name))" -ForegroundColor Green
}

# Make sure the required providers are registered (no-op if already done)
foreach ($ns in @("Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.OperationalInsights")) {
    $state = az provider show -n $ns --query registrationState -o tsv 2>$null
    if ($state -ne "Registered") {
        Write-Host "[deploy] registering provider $ns ..." -ForegroundColor Cyan
        az provider register -n $ns --wait | Out-Null
    }
}

# Make sure containerapp extension is installed (no-op if already done)
az extension add --name containerapp --upgrade --only-show-errors 2>$null | Out-Null

# ---------------------------------------------------------------------------
# 2. Discover RG from Fabric capacity (unless overridden)
# ---------------------------------------------------------------------------
Write-Host "[deploy] looking up Fabric capacity '$($env:FABRIC_CAPACITY_NAME)' (RG + region)" -ForegroundColor Cyan
# Note: --name filter is not honoured server-side for Microsoft.Fabric/capacities,
# so we list all and filter client-side via JMESPath.
$capInfo = az resource list `
    --resource-type "Microsoft.Fabric/capacities" `
    --query "[?name=='$($env:FABRIC_CAPACITY_NAME)'] | [0].{rg:resourceGroup,loc:location}" -o json | ConvertFrom-Json
if (-not $capInfo) {
    throw "Could not find a Microsoft.Fabric/capacities resource named '$($env:FABRIC_CAPACITY_NAME)' in this subscription."
}
if (-not $RgName)    { $RgName    = $capInfo.rg }
if (-not $Location)  { $Location  = $capInfo.loc }
Write-Host "[deploy] using resource group: $RgName  (region: $Location)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. ACR (create or reuse)
# ---------------------------------------------------------------------------
if (-not $AcrName) {
    # Try to find an existing ACR in the RG first, otherwise create one.
    $existing = az acr list -g $RgName --query "[0].name" -o tsv
    if ($existing) {
        $AcrName = $existing
        Write-Host "[deploy] reusing existing ACR: $AcrName" -ForegroundColor Green
    } else {
        $suffix = -join ((48..57 + 97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
        $AcrName = "acrsim$suffix"
        Write-Host "[deploy] creating ACR $AcrName ..." -ForegroundColor Cyan
        az acr create -g $RgName -n $AcrName --sku Basic --admin-enabled true --location $Location | Out-Null
    }
} else {
    $exists = az acr show -n $AcrName 2>$null
    if (-not $exists) {
        Write-Host "[deploy] creating ACR $AcrName ..." -ForegroundColor Cyan
        az acr create -g $RgName -n $AcrName --sku Basic --admin-enabled true --location $Location | Out-Null
    }
}

# ---------------------------------------------------------------------------
# 4. Build image (remote build via ACR Tasks — no local Docker needed)
# ---------------------------------------------------------------------------
$image = "$AcrName.azurecr.io/simulator:$ImageTag"
Write-Host "[deploy] az acr build -> $image (this is the longest step, ~3-5 min on first build)" -ForegroundColor Cyan
Push-Location $PSScriptRoot
try {
    az acr build --registry $AcrName --image "simulator:$ImageTag" --file Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw "acr build failed" }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# 5. Container Apps environment
# ---------------------------------------------------------------------------
$envExists = az containerapp env show -g $RgName -n $EnvName 2>$null
if (-not $envExists) {
    Write-Host "[deploy] creating Container Apps env '$EnvName' ($Location) ..." -ForegroundColor Cyan
    az containerapp env create -g $RgName -n $EnvName --location $Location | Out-Null
}

# ---------------------------------------------------------------------------
# 6. Container app
# ---------------------------------------------------------------------------
$acrUser = az acr credential show -n $AcrName --query username -o tsv
$acrPass = az acr credential show -n $AcrName --query "passwords[0].value" -o tsv

$envVars = @(
    "EVENTSTREAM_CONNECTION_STRING=secretref:eventstream-conn",
    "PYTHONUNBUFFERED=1",
    "SIM_MACHINES=$Machines",
    "SIM_RATE=$Rate",
    "SIM_ANOMALY_PROB=$AnomalyProb"
)

$appExists = az containerapp show -g $RgName -n $AppName 2>$null
if ($appExists) {
    Write-Host "[deploy] updating existing container app '$AppName' ..." -ForegroundColor Cyan
    # Refresh secret + image
    az containerapp secret set -g $RgName -n $AppName `
        --secrets "eventstream-conn=$($env:EVENTSTREAM_CONNECTION_STRING)" | Out-Null
    az containerapp registry set -g $RgName -n $AppName `
        --server "$AcrName.azurecr.io" --username $acrUser --password $acrPass | Out-Null
    # Always force a new revision so env-var only changes take effect even
    # when the image digest hasn't moved (otherwise ACA reports
    # "no changes detected" and silently skips the update).
    $revSuffix = "v$(Get-Date -Format 'yyMMddHHmm')"
    az containerapp update -g $RgName -n $AppName `
        --image $image `
        --revision-suffix $revSuffix `
        --set-env-vars @envVars | Out-Null
} else {
    Write-Host "[deploy] creating container app '$AppName' ..." -ForegroundColor Cyan
    az containerapp create `
        -g $RgName -n $AppName `
        --environment $EnvName `
        --image $image `
        --registry-server "$AcrName.azurecr.io" `
        --registry-username $acrUser `
        --registry-password $acrPass `
        --secrets "eventstream-conn=$($env:EVENTSTREAM_CONNECTION_STRING)" `
        --env-vars @envVars `
        --min-replicas 1 --max-replicas 1 `
        --cpu 0.25 --memory 0.5Gi | Out-Null
}

Write-Host ""
Write-Host "[deploy] Done." -ForegroundColor Green
Write-Host "  Tail logs:    az containerapp logs tail -g $RgName -n $AppName --follow" -ForegroundColor Yellow
Write-Host "  Restart:      az containerapp revision restart -g $RgName -n $AppName --revision (az containerapp revision list -g $RgName -n $AppName --query '[0].name' -o tsv)" -ForegroundColor Yellow
Write-Host "  Teardown:     pwsh ./simulator-cloud/teardown.ps1" -ForegroundColor Yellow
