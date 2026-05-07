<#
.SYNOPSIS
    Bootstraps a Microsoft Fabric workspace and items using the Fabric CLI.

.DESCRIPTION
    - Loads configuration from .env (gitignored)
    - Authenticates to Fabric via device code (browser prompt on first run)
    - Creates / updates: Workspace, Lakehouse, Notebook, Data Pipeline,
      Eventhouse and a KQL Database inside it.

.EXAMPLE
    pwsh ./scripts/deploy.ps1
#>

[CmdletBinding()]
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot '..' '.env')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- Load helpers --------------------------------------------------------
. (Join-Path $PSScriptRoot 'lib' 'env.ps1')
. (Join-Path $PSScriptRoot 'lib' 'fabric.ps1')

# --- Pre-flight ----------------------------------------------------------
Assert-FabCli
Import-DotEnv -Path $EnvFile
Assert-EnvVars @(
    'FABRIC_TENANT_ID',
    'FABRIC_CAPACITY_NAME',
    'FABRIC_WORKSPACE_NAME',
    'FABRIC_LAKEHOUSE_NAME',
    'FABRIC_NOTEBOOK_NAME',
    'FABRIC_PIPELINE_NAME',
    'FABRIC_EVENTHOUSE_NAME',
    'FABRIC_KQLDB_NAME'
)

# --- Authenticate (device code) ------------------------------------------
Write-Host "Authenticating to Fabric (device code)..." -ForegroundColor Cyan
# `fab auth login` defaults to interactive device-code flow when no
# --client-id / --client-secret are supplied.
Invoke-Fab auth login --tenant $env:FABRIC_TENANT_ID | Out-Null

# --- Workspace -----------------------------------------------------------
$ws = New-FabricWorkspace `
        -Name         $env:FABRIC_WORKSPACE_NAME `
        -CapacityName $env:FABRIC_CAPACITY_NAME

# --- Items ---------------------------------------------------------------
Write-Host "Provisioning items in $ws..." -ForegroundColor Cyan

New-FabricItem -Workspace $ws -Name $env:FABRIC_LAKEHOUSE_NAME -Type Lakehouse    | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_PIPELINE_NAME  -Type DataPipeline | Out-Null

# Eventhouse first; KQL database is nested inside it.
$ehPath = New-FabricItem -Workspace $ws -Name $env:FABRIC_EVENTHOUSE_NAME -Type Eventhouse
New-FabricItem `
    -Workspace $ws `
    -Name      $env:FABRIC_KQLDB_NAME `
    -Type      KQLDatabase `
    -Params    @{ parentEventhouseName = $env:FABRIC_EVENTHOUSE_NAME } | Out-Null

# Notebook: import a definition folder so the code is in source control.
$notebookSrc = Join-Path $PSScriptRoot '..' 'items' "$($env:FABRIC_NOTEBOOK_NAME).Notebook"
if (Test-Path $notebookSrc) {
    Import-FabricItem -Workspace $ws -Path $notebookSrc | Out-Null
} else {
    Write-Host "  No notebook definition at $notebookSrc - creating empty notebook." -ForegroundColor Yellow
    New-FabricItem -Workspace $ws -Name $env:FABRIC_NOTEBOOK_NAME -Type Notebook | Out-Null
}

Write-Host "`nDone. Workspace ready: $ws" -ForegroundColor Green
