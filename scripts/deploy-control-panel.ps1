<#
.SYNOPSIS
    One-shot, repeatable deploy of the entire control-panel stack:
      1. Static Web App (infra/swa.bicep)            -> public URL
      2. Entra ID app registration + sign-in gating  -> client id / scope
      3. Container App control API (Entra-protected)  -> backend FQDN
      4. webapp/ publish (config.js generated, MSAL vendored)

    Every step is idempotent — safe to re-run after any change. No manual
    portal steps are required.

.DESCRIPTION
    Reads tenant / capacity / subscription from .env (see .env.example).
    The resource group is discovered from the Fabric capacity unless -RgName
    is supplied.

.EXAMPLE
    pwsh ./scripts/deploy-control-panel.ps1

.EXAMPLE
    pwsh ./scripts/deploy-control-panel.ps1 -ImageTag auth2 -RgName rg-fabric-demo
#>
[CmdletBinding()]
param(
    [string]$RgName,
    [string]$Location,
    [string]$SwaName     = "swa-anomaly-sim",
    [string]$SwaLocation = "westeurope",
    [string]$AppName     = "ca-simulator",
    [string]$ImageTag    = "auth1",
    [string]$AppDisplayName = "Anomaly Sim Control Panel",
    [int]   $Machines    = 3,
    [switch]$AllowApiKey
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot/..").Path

. (Join-Path $PSScriptRoot "lib/env.ps1")
Import-DotEnv
Assert-EnvVars @("FABRIC_TENANT_ID", "FABRIC_CAPACITY_NAME")

# --- Login (device code, idempotent) --------------------------------------
$ctx = az account show 2>$null | ConvertFrom-Json
if (-not $ctx -or $ctx.tenantId -ne $env:FABRIC_TENANT_ID) {
    Write-Host "[stack] az login --use-device-code --tenant $($env:FABRIC_TENANT_ID)" -ForegroundColor Cyan
    az login --use-device-code --tenant $env:FABRIC_TENANT_ID | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
}

# --- Discover resource group / region from the Fabric capacity ------------
if (-not $RgName -or -not $Location) {
    $cap = az resource list --resource-type "Microsoft.Fabric/capacities" `
        --query "[?name=='$($env:FABRIC_CAPACITY_NAME)'] | [0].{rg:resourceGroup,loc:location}" -o json | ConvertFrom-Json
    if (-not $cap) { throw "Fabric capacity '$($env:FABRIC_CAPACITY_NAME)' not found. Pass -RgName/-Location." }
    if (-not $RgName)   { $RgName = $cap.rg }
    if (-not $Location) { $Location = $cap.loc }
}
Write-Host "[stack] resource group: $RgName ($Location)" -ForegroundColor Green

# ==========================================================================
# 1. Static Web App (create/reuse) -> public URL (needed for redirect URI)
# ==========================================================================
az extension add --name staticwebapp --upgrade --only-show-errors 2>$null | Out-Null
Write-Host "[stack] (1/4) ensuring Static Web App '$SwaName'" -ForegroundColor Cyan
az deployment group create -g $RgName `
    --template-file (Join-Path $root "infra/swa.bicep") `
    --parameters name=$SwaName location=$SwaLocation --only-show-errors | Out-Null
if ($LASTEXITCODE -ne 0) { throw "swa.bicep deployment failed" }
$swaHost = az staticwebapp show -n $SwaName -g $RgName --query "defaultHostname" -o tsv
$swaUrl  = "https://$swaHost"
Write-Host "[stack] SWA: $swaUrl" -ForegroundColor Green

# ==========================================================================
# 2. App registration + sign-in gating -> client id / scope
# ==========================================================================
Write-Host "[stack] (2/4) configuring app registration" -ForegroundColor Cyan
$appreg = & (Join-Path $PSScriptRoot "setup-app-registration.ps1") `
    -DisplayName $AppDisplayName `
    -SpaRedirectUris @($swaUrl, "http://localhost:4280") `
    -TenantId $env:FABRIC_TENANT_ID
$clientId = $appreg.ClientId
$scope    = $appreg.Scope

# ==========================================================================
# 3. Container App control API (Entra-protected) -> backend FQDN
# ==========================================================================
Write-Host "[stack] (3/4) deploying container control API" -ForegroundColor Cyan
$ctrlArgs = @{
    RgName       = $RgName
    AppName      = $AppName
    ImageTag     = $ImageTag
    Machines     = $Machines
    EnableControl = $true
    AuthTenantId = $env:FABRIC_TENANT_ID
    AuthClientId = $clientId
    CorsOrigins  = $swaUrl
}
if ($AllowApiKey -and $env:SIM_CONTROL_API_KEY) {
    $ctrlArgs.AuthAllowApiKey = $true
    $ctrlArgs.ControlApiKey   = $env:SIM_CONTROL_API_KEY
}
& (Join-Path $root "simulator-cloud/deploy.ps1") @ctrlArgs
$backendFqdn = az containerapp show -g $RgName -n $AppName `
    --query "properties.configuration.ingress.fqdn" -o tsv
$backendUrl  = "https://$backendFqdn"
Write-Host "[stack] backend: $backendUrl" -ForegroundColor Green

# ==========================================================================
# 4. Publish the webapp (config.js generated, MSAL vendored)
# ==========================================================================
Write-Host "[stack] (4/4) publishing webapp" -ForegroundColor Cyan
& (Join-Path $root "webapp/deploy.ps1") `
    -RgName $RgName -Name $SwaName -Location $SwaLocation `
    -BackendUrl $backendUrl -ContainerApp $AppName `
    -TenantId $env:FABRIC_TENANT_ID -ClientId $clientId -Scope $scope

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Control panel deployed (Entra-gated)." -ForegroundColor Green
Write-Host "   Panel:    $swaUrl" -ForegroundColor Yellow
Write-Host "   Backend:  $backendUrl" -ForegroundColor Yellow
Write-Host "   ClientId: $clientId" -ForegroundColor Yellow
Write-Host "   Scope:    $scope" -ForegroundColor Yellow
Write-Host " Only assigned tenant users can sign in." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
