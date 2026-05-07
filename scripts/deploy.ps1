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
    'FABRIC_NOTEBOOK_FEATURES_NAME',
    'FABRIC_NOTEBOOK_TRAIN_NAME',
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
New-FabricItem -Workspace $ws -Name $env:FABRIC_EVENTHOUSE_NAME  -Type Eventhouse  | Out-Null
New-FabricItem `
    -Workspace $ws `
    -Name      $env:FABRIC_KQLDB_NAME `
    -Type      KQLDatabase `
    -Params    @{ parentEventhouseName = $env:FABRIC_EVENTHOUSE_NAME } | Out-Null

# Cold path: Lakehouse + Spark Environment ---------------------------------
New-FabricItem -Workspace $ws -Name $env:FABRIC_LAKEHOUSE_NAME   -Type Lakehouse   | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_ENVIRONMENT_NAME -Type Environment | Out-Null

# Notebooks: created blank. Source content for each is checked in under
# items/<name>.Notebook/notebook-content.py and is intended to be loaded
# either via Fabric Git integration (recommended) or pasted into the
# notebook in the Fabric portal. `fab import` of the Git source format
# (.py) is not supported - it expects .ipynb JSON.
$notebookNames = @(
    $env:FABRIC_NOTEBOOK_FEATURES_NAME,
    $env:FABRIC_NOTEBOOK_TRAIN_NAME,
    $env:FABRIC_NOTEBOOK_REGISTER_NAME
)
foreach ($nb in $notebookNames) {
    New-FabricItem -Workspace $ws -Name $nb -Type Notebook | Out-Null
}

# Orchestration + alerting + BI --------------------------------------------
New-FabricItem -Workspace $ws -Name $env:FABRIC_PIPELINE_NAME       -Type DataPipeline  | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_ACTIVATOR_NAME      -Type Reflex        | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_SEMANTIC_MODEL_NAME -Type SemanticModel | Out-Null
New-FabricItem -Workspace $ws -Name $env:FABRIC_REPORT_NAME         -Type Report        | Out-Null

Write-Host "`nDone. Workspace ready: $ws" -ForegroundColor Green
Write-Host "Next: run the KQL scripts in ./kql against $($env:FABRIC_KQLDB_NAME) and wire the Eventstream sources/destinations in the portal. See docs/architecture.md." -ForegroundColor Cyan
