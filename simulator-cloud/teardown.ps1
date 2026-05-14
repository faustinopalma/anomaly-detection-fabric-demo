<#
.SYNOPSIS
    Tear down the simulator container app (and optionally the ACA env / ACR).

.EXAMPLE
    pwsh ./simulator-cloud/teardown.ps1
    pwsh ./simulator-cloud/teardown.ps1 -RemoveEnv -RemoveAcr
#>
[CmdletBinding()]
param(
    [string]$RgName,
    [string]$EnvName = "cae-anomalydet",
    [string]$AppName = "ca-simulator",
    [string]$AcrName,
    [switch]$RemoveEnv,
    [switch]$RemoveAcr
)

$ErrorActionPreference = "Stop"

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

if (-not $RgName) {
    $RgName = az resource list `
        --resource-type "Microsoft.Fabric/capacities" `
        --query "[?name=='$($env:FABRIC_CAPACITY_NAME)'].resourceGroup | [0]" -o tsv
    if (-not $RgName) { throw "Cannot resolve resource group from FABRIC_CAPACITY_NAME." }
}
Write-Host "[teardown] RG: $RgName" -ForegroundColor Cyan

if (az containerapp show -g $RgName -n $AppName 2>$null) {
    Write-Host "[teardown] deleting container app '$AppName' ..." -ForegroundColor Yellow
    az containerapp delete -g $RgName -n $AppName --yes | Out-Null
} else {
    Write-Host "[teardown] container app '$AppName' not found, skipping" -ForegroundColor DarkGray
}

if ($RemoveEnv) {
    if (az containerapp env show -g $RgName -n $EnvName 2>$null) {
        Write-Host "[teardown] deleting ACA env '$EnvName' ..." -ForegroundColor Yellow
        az containerapp env delete -g $RgName -n $EnvName --yes | Out-Null
    }
}

if ($RemoveAcr) {
    if (-not $AcrName) {
        $AcrName = az acr list -g $RgName --query "[0].name" -o tsv
    }
    if ($AcrName -and (az acr show -n $AcrName 2>$null)) {
        Write-Host "[teardown] deleting ACR '$AcrName' ..." -ForegroundColor Yellow
        az acr delete -n $AcrName -g $RgName --yes | Out-Null
    }
}

Write-Host "[teardown] done." -ForegroundColor Green
