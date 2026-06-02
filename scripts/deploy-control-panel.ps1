<#
.SYNOPSIS
    One-shot, repeatable deploy of the entire control-panel stack — now served
    end-to-end from the Container App (no Static Web App):
      1. Container App control API + static panel (external ingress) -> FQDN
      2. Entra ID app registration + sign-in gating (redirect = the FQDN)
      3. Container App redeploy wired to the app registration (Entra-gated)

    Every step is idempotent — safe to re-run after any change. No manual
    portal steps are required.

.DESCRIPTION
    Reads tenant / capacity / subscription from .env (see .env.example).
    The resource group is discovered from the Fabric capacity unless -RgName
    is supplied.

    The control panel (webapp/) is baked into the container image and served
    same-origin by the FastAPI control server, so there is a single public URL
    and a single redirect URI to register.

.EXAMPLE
    pwsh ./scripts/deploy-control-panel.ps1

.EXAMPLE
    pwsh ./scripts/deploy-control-panel.ps1 -ImageTag web1 -RgName rg-fabric-demo
#>
[CmdletBinding()]
param(
    [string]$RgName,
    [string]$Location,
    [string]$AppName     = "ca-simulator",
    [string]$ImageTag    = "web1",
    [string]$AppDisplayName = "Anomaly Sim Control Panel",
    [int]   $Machines    = 4,
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
# 1. Ensure the Container App exists (so we know its public FQDN for the
#    redirect URI). On first run we bootstrap-create it (build + external
#    ingress, no auth yet); on re-runs we reuse the existing FQDN.
# ==========================================================================
$backendFqdn = az containerapp show -g $RgName -n $AppName `
    --query "properties.configuration.ingress.fqdn" -o tsv 2>$null
$bootstrapped = $false
if (-not $backendFqdn) {
    Write-Host "[stack] (1/3) bootstrapping container app '$AppName' to obtain its FQDN" -ForegroundColor Cyan
    & (Join-Path $root "simulator-cloud/deploy.ps1") `
        -RgName $RgName -AppName $AppName -ImageTag $ImageTag -Machines $Machines `
        -EnableControl
    $backendFqdn = az containerapp show -g $RgName -n $AppName `
        --query "properties.configuration.ingress.fqdn" -o tsv
    $bootstrapped = $true
} else {
    Write-Host "[stack] (1/3) reusing existing container app FQDN" -ForegroundColor Green
}
$backendUrl = "https://$backendFqdn"
Write-Host "[stack] panel/backend: $backendUrl" -ForegroundColor Green

# ==========================================================================
# 2. App registration + sign-in gating (redirect URI = the container URL)
# ==========================================================================
Write-Host "[stack] (2/3) configuring app registration" -ForegroundColor Cyan
$appreg = & (Join-Path $PSScriptRoot "setup-app-registration.ps1") `
    -DisplayName $AppDisplayName `
    -SpaRedirectUris @($backendUrl, "http://localhost:8080") `
    -TenantId $env:FABRIC_TENANT_ID
$clientId = $appreg.ClientId
$scope    = $appreg.Scope

# ==========================================================================
# 3. Redeploy the container wired to Entra auth. Skip the rebuild only when we
#    just built the image in the bootstrap step above.
# ==========================================================================
Write-Host "[stack] (3/3) wiring Entra auth onto the container" -ForegroundColor Cyan
$ctrlArgs = @{
    RgName        = $RgName
    AppName       = $AppName
    ImageTag      = $ImageTag
    Machines      = $Machines
    EnableControl = $true
    AuthTenantId  = $env:FABRIC_TENANT_ID
    AuthClientId  = $clientId
}
if ($bootstrapped) { $ctrlArgs.SkipBuild = $true }
if ($AllowApiKey -and $env:SIM_CONTROL_API_KEY) {
    $ctrlArgs.AuthAllowApiKey = $true
    $ctrlArgs.ControlApiKey   = $env:SIM_CONTROL_API_KEY
}
& (Join-Path $root "simulator-cloud/deploy.ps1") @ctrlArgs

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Control panel deployed (Entra-gated, served from the container)." -ForegroundColor Green
Write-Host "   Panel:    $backendUrl" -ForegroundColor Yellow
Write-Host "   ClientId: $clientId" -ForegroundColor Yellow
Write-Host "   Scope:    $scope" -ForegroundColor Yellow
Write-Host " Only assigned tenant users can sign in." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
