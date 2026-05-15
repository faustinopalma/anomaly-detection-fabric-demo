<#
.SYNOPSIS
    Bootstraps a Microsoft Fabric workspace and the items needed for the
    factory anomaly-detection demo, using the Fabric CLI.

.DESCRIPTION
    - Loads configuration from .env (gitignored)
    - Authenticates to Fabric via device code (browser prompt on first run)
    - Creates / updates (idempotent):
        * Workspace (assigned to the configured capacity)
        * Eventstream                       (ingestion endpoint for machines)
        * Eventhouse + KQL Database         (hot path: telemetry + ONNX scoring)
        * Lakehouse                         (cold path: bronze/silver/gold + ONNX artifacts)
        * Environment                       (pinned Spark libs for training notebooks)
        * 3 Notebooks                       (features / train+export ONNX / register KQL scorer)
        * Data Pipeline                     (orchestrates retraining)
        * Reflex (Activator)                (alerts on anomalies)
        * Semantic Model + Report           (BI surface)

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
    'FABRIC_EVENTSTREAM_NAME',
    'FABRIC_EVENTHOUSE_NAME',
    'FABRIC_KQLDB_NAME',
    'FABRIC_LAKEHOUSE_NAME',
    'FABRIC_ENVIRONMENT_NAME',
    'FABRIC_NOTEBOOK_REGISTER_NAME',
    'FABRIC_PIPELINE_NAME',
    'FABRIC_ACTIVATOR_NAME',
    'FABRIC_SEMANTIC_MODEL_NAME',
    'FABRIC_REPORT_NAME'
)

# --- Authenticate (device code) ------------------------------------------
# Skip if already authenticated; `fab auth login` requires an interactive
# console (Windows broker) and will fail otherwise.
$authStatus = & fab auth status 2>&1
if ($LASTEXITCODE -eq 0 -and ($authStatus -match 'Logged in')) {
    Write-Host "Already authenticated to Fabric - skipping login." -ForegroundColor DarkGray
} else {
    Write-Host "Authenticating to Fabric (interactive)..." -ForegroundColor Cyan
    & fab auth login --tenant $env:FABRIC_TENANT_ID
    if ($LASTEXITCODE -ne 0) {
        throw "fab auth login failed. Run it manually in a regular terminal first: fab auth login --tenant $env:FABRIC_TENANT_ID"
    }
}

# --- Workspace -----------------------------------------------------------
$ws = New-FabricWorkspace `
        -Name         $env:FABRIC_WORKSPACE_NAME `
        -CapacityName $env:FABRIC_CAPACITY_NAME

# --- Items ---------------------------------------------------------------
Write-Host "Provisioning items in $ws..." -ForegroundColor Cyan

# Ingestion ----------------------------------------------------------------
New-FabricItem -Workspace $ws -Name $env:FABRIC_EVENTSTREAM_NAME -Type Eventstream | Out-Null

# Hot path: Eventhouse + KQL DB --------------------------------------------
# IMPORTANT: the KQL Database must be linked to the Eventhouse via
# `parentEventhouseItemId` (the *item id*, not the name) plus
# `databaseType=ReadWrite`. Passing only `parentEventhouseName` is silently
# ignored by the Fabric REST API: the call still succeeds, but Fabric
# auto-creates a second Eventhouse named "<dbname>_auto" and puts the DB
# inside it, leaving the Eventhouse we just created empty and orphaned.
New-FabricItem -Workspace $ws -Name $env:FABRIC_EVENTHOUSE_NAME  -Type Eventhouse  | Out-Null
$ehId = Get-FabricItemId -Workspace $ws -Name $env:FABRIC_EVENTHOUSE_NAME -Type Eventhouse
New-FabricItem `
    -Workspace $ws `
    -Name      $env:FABRIC_KQLDB_NAME `
    -Type      KQLDatabase `
    -Params    @{
        databaseType            = 'ReadWrite'
        parentEventhouseItemId  = $ehId
    } | Out-Null

# Cold path: Lakehouse + Spark Environment ---------------------------------
New-FabricItem -Workspace $ws -Name $env:FABRIC_LAKEHOUSE_NAME   -Type Lakehouse   | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_ENVIRONMENT_NAME -Type Environment | Out-Null

# Notebooks: only the legacy `nb_register_kql_scorer` scaffold is created
# here (still in use to re-apply kql/*.kql). The active training notebooks
# (01_simulator_dev / 02_train_univariate_ae / 03_train_multivariate_ae)
# are published from notebooks/ via tools/upload_notebook.py.
New-FabricItem -Workspace $ws -Name $env:FABRIC_NOTEBOOK_REGISTER_NAME -Type Notebook | Out-Null

# Orchestration + alerting + BI --------------------------------------------
New-FabricItem -Workspace $ws -Name $env:FABRIC_PIPELINE_NAME       -Type DataPipeline  | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_ACTIVATOR_NAME      -Type Reflex        | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_SEMANTIC_MODEL_NAME -Type SemanticModel | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_REPORT_NAME         -Type Report        | Out-Null

Write-Host "`nDone. Workspace ready: $ws" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Add a CustomEndpoint source to the eventstream and write its" -ForegroundColor Cyan
Write-Host "     connection string into .env (no portal needed):" -ForegroundColor Cyan
Write-Host "         python tools/01_setup_eventstream_source.py" -ForegroundColor Yellow
Write-Host "  2. Start the telemetry simulator (locally for dev, or as ACA for always-on):" -ForegroundColor Cyan
Write-Host "         python simulator-local/simulate_machines.py" -ForegroundColor Yellow
Write-Host "         pwsh ./simulator-cloud/deploy.ps1" -ForegroundColor Yellow
Write-Host "  3. Run the KQL scripts in ./kql against $($env:FABRIC_KQLDB_NAME)" -ForegroundColor Cyan
Write-Host "     and wire any remaining destinations. See docs/architecture.md." -ForegroundColor Cyan
