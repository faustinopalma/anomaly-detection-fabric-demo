# Creates a Microsoft Fabric capacity using the Bicep template under
# infra/fabric-capacity.bicep.
#
# Reads defaults from .env when present; CLI parameters override env vars.
#
# Prerequisites:
#   - Azure CLI logged in (`az login`) with access to the target subscription.
#   - The Microsoft.Fabric resource provider registered on the subscription:
#       az provider register --namespace Microsoft.Fabric
#
# Usage:
#   pwsh ./scripts/create-capacity.ps1 `
#       -SubscriptionId <subId> `
#       -ResourceGroup  rg-fabric-demo `
#       -Location       westeurope `
#       -CapacityName   anomalydetection2 `
#       -Sku            F4 `
#       -AdminMembers   user1@contoso.com,user2@contoso.com
#
# After creation, set FABRIC_CAPACITY_NAME=<CapacityName> in .env and run
# ./scripts/deploy.ps1 to provision the workspace and items.

[CmdletBinding()]
param(
    [string]$SubscriptionId,
    [string]$ResourceGroup,
    [string]$Location,
    [string]$CapacityName,
    [ValidateSet('F2','F4','F8','F16','F32','F64','F128','F256','F512','F1024','F2048')]
    [string]$Sku,
    [string[]]$AdminMembers,
    [hashtable]$Tags       = @{ project = 'anomaly-detection-demo' }
)

$ErrorActionPreference = 'Stop'

# --- Load .env defaults --------------------------------------------------
# Reuse the shared loader so quoting / comments / precedence semantics stay
# consistent with scripts/deploy.ps1.
. (Join-Path $PSScriptRoot 'lib/env.ps1')
$repoRoot = Split-Path $PSScriptRoot -Parent
$envFile  = Join-Path $repoRoot '.env'
if (Test-Path $envFile) { Import-DotEnv -Path $envFile }

# Resolve every input from: CLI parameter > .env value > hardcoded fallback.
if (-not $CapacityName)   { $CapacityName   = $env:FABRIC_CAPACITY_NAME }
if (-not $CapacityName)   { throw "CapacityName not provided and FABRIC_CAPACITY_NAME not set in .env" }

if (-not $ResourceGroup)  { $ResourceGroup  = $env:AZURE_RESOURCE_GROUP }
if (-not $ResourceGroup)  { throw "ResourceGroup not provided and AZURE_RESOURCE_GROUP not set in .env" }

if (-not $Location)       { $Location       = $env:AZURE_LOCATION }
if (-not $Location)       { $Location       = 'italynorth' }

if (-not $Sku)            { $Sku            = $env:FABRIC_CAPACITY_SKU }
if (-not $Sku)            { $Sku            = 'F4' }
if ($Sku -notin @('F2','F4','F8','F16','F32','F64','F128','F256','F512','F1024','F2048')) {
    throw "Invalid SKU '$Sku'. Allowed: F2, F4, F8, F16, F32, F64, F128, F256, F512, F1024, F2048."
}

if (-not $AdminMembers -or $AdminMembers.Count -eq 0) {
    if ($env:FABRIC_CAPACITY_ADMINS) {
        $AdminMembers = $env:FABRIC_CAPACITY_ADMINS -split '[,;]' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    }
}
if (-not $AdminMembers -or $AdminMembers.Count -eq 0) {
    # Fall back to the signed-in user.
    $AdminMembers = @((az account show --query user.name -o tsv))
    Write-Host "No -AdminMembers provided; defaulting to signed-in user: $($AdminMembers -join ', ')" -ForegroundColor Yellow
}

if (-not $SubscriptionId) { $SubscriptionId = $env:AZURE_SUBSCRIPTION_ID }
# (SubscriptionId is optional - if omitted we use the current az context.)

# --- Az CLI sanity -------------------------------------------------------
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI not found. Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
}

# --- Device code login --------------------------------------------------
# Always (re)authenticate against the target tenant via device code so the
# script works on machines without a browser / cached creds.
$tenantArg = @()
if ($env:FABRIC_TENANT_ID) { $tenantArg = @('--tenant', $env:FABRIC_TENANT_ID) }

$needLogin = $true
try {
    $current = az account show -o json 2>$null | ConvertFrom-Json
    if ($current) {
        if ($env:FABRIC_TENANT_ID -and $current.tenantId -ne $env:FABRIC_TENANT_ID) {
            Write-Host "Current az context is on tenant $($current.tenantId); switching to $($env:FABRIC_TENANT_ID) via device code..." -ForegroundColor Yellow
        } elseif ($SubscriptionId -and $current.id -ne $SubscriptionId) {
            Write-Host "Current az context is on subscription $($current.id); switching to $SubscriptionId via device code..." -ForegroundColor Yellow
        } else {
            $needLogin = $false
        }
    }
} catch { $needLogin = $true }

if ($needLogin) {
    Write-Host "Signing in to Azure with device code..." -ForegroundColor Cyan
    az login --use-device-code @tenantArg | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
}

if ($SubscriptionId) {
    az account set --subscription $SubscriptionId | Out-Null
}
$ctx = az account show -o json | ConvertFrom-Json
Write-Host "Subscription : $($ctx.name)  ($($ctx.id))" -ForegroundColor Cyan
Write-Host "Tenant       : $($ctx.tenantId)"             -ForegroundColor Cyan
Write-Host "RG / Location: $ResourceGroup / $Location"   -ForegroundColor Cyan
Write-Host "Capacity     : $CapacityName  (SKU $Sku)"    -ForegroundColor Cyan
Write-Host "Admins       : $($AdminMembers -join ', ')"  -ForegroundColor Cyan
Write-Host ""

# --- Resource provider ---------------------------------------------------
$rpState = az provider show --namespace Microsoft.Fabric --query registrationState -o tsv 2>$null
if ($rpState -ne 'Registered') {
    Write-Host "Registering Microsoft.Fabric provider (this can take a minute)..." -ForegroundColor Cyan
    az provider register --namespace Microsoft.Fabric | Out-Null
    do {
        Start-Sleep -Seconds 5
        $rpState = az provider show --namespace Microsoft.Fabric --query registrationState -o tsv
        Write-Host "  state: $rpState" -ForegroundColor DarkGray
    } while ($rpState -ne 'Registered')
}

# --- Resource group ------------------------------------------------------
$rgExists = az group exists --name $ResourceGroup
if ($rgExists -eq 'false') {
    Write-Host "Creating resource group $ResourceGroup in $Location..." -ForegroundColor Cyan
    az group create --name $ResourceGroup --location $Location | Out-Null
}

# --- Bicep deployment ----------------------------------------------------
$bicepPath = Join-Path $repoRoot 'infra/fabric-capacity.bicep'
if (-not (Test-Path $bicepPath)) { throw "Bicep template not found: $bicepPath" }

$paramsJson = @{
    capacityName = @{ value = $CapacityName }
    location     = @{ value = $Location     }
    sku          = @{ value = $Sku          }
    adminMembers = @{ value = $AdminMembers }
    tags         = @{ value = $Tags         }
} | ConvertTo-Json -Depth 5 -Compress

$tmpParams = New-TemporaryFile
$paramsJson | Set-Content -LiteralPath $tmpParams -Encoding utf8

try {
    Write-Host "`nDeploying Fabric capacity (this can take 1-3 minutes)..." -ForegroundColor Cyan
    $result = az deployment group create `
        --resource-group $ResourceGroup `
        --name "fabric-capacity-$CapacityName" `
        --template-file $bicepPath `
        --parameters "@$tmpParams" `
        -o json | ConvertFrom-Json
} finally {
    Remove-Item -LiteralPath $tmpParams -ErrorAction SilentlyContinue
}

$out = $result.properties.outputs
Write-Host ""
Write-Host "Fabric capacity ready:" -ForegroundColor Green
Write-Host "  id       : $($out.capacityId.value)"
Write-Host "  name     : $($out.capacityName.value)"
Write-Host "  sku      : $($out.sku.value)"
Write-Host "  location : $($out.location.value)"
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Set in .env:  FABRIC_CAPACITY_NAME=$($out.capacityName.value)"
Write-Host "  2. Run:          pwsh ./scripts/deploy.ps1"
