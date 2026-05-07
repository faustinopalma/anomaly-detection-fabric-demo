# Thin wrappers around the Fabric CLI (`fab`) that make scripts idempotent
# and easier to read. All functions write progress to the host and throw on
# unexpected failures.

function Assert-FabCli {
    if (-not (Get-Command fab -ErrorAction SilentlyContinue)) {
        throw "Fabric CLI not found. Install with: pip install --upgrade ms-fabric-cli"
    }
}

function Invoke-Fab {
    # Runs `fab` and throws if exit code is non-zero. Returns stdout lines.
    # Arguments must be passed as a single array to avoid PowerShell's
    # automatic parameter binding (e.g. -P clashing with -PipelineVariable).
    param([Parameter(Mandatory)][string[]]$FabArgs)

    Write-Host "  > fab $($FabArgs -join ' ')" -ForegroundColor DarkGray
    $output = & fab @FabArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $output | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        throw "fab $($FabArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

function Test-FabPath {
    param([Parameter(Mandatory)][string]$Path)
    # `fab exists` always exits 0 and prints something like "* true" / "* false"
    # (the leading "* " is a TTY marker stripped when stdout is captured).
    $out = & fab exists $Path 2>&1 | Out-String
    return ($out -match '(?im)^\s*(\*\s*)?true\s*$')
}

function New-FabricWorkspace {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$CapacityName
    )

    $wsPath = "/$Name.Workspace"
    if (Test-FabPath $wsPath) {
        Write-Host "Workspace '$Name' already exists - skipping create." -ForegroundColor Yellow
    } else {
        Write-Host "Creating workspace '$Name' on capacity '$CapacityName'..." -ForegroundColor Cyan
        Invoke-Fab -FabArgs @('create', $wsPath, '-P', "capacityName=$CapacityName") | Out-Null
    }
    return $wsPath
}

function New-FabricItem {
    param(
        [Parameter(Mandatory)][string]$Workspace,   # e.g. /my-ws.Workspace
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet(
            'Lakehouse','Notebook','DataPipeline','Eventhouse','KQLDatabase',
            'KQLQueryset','Eventstream','Reflex',
            'Warehouse','SemanticModel','Report','Environment','MLModel','MLExperiment'
        )][string]$Type,
        [hashtable]$Params
    )

    $itemPath = "$Workspace/$Name.$Type"
    if (Test-FabPath $itemPath) {
        Write-Host "  $Type '$Name' already exists - skipping." -ForegroundColor Yellow
        return $itemPath
    }

    Write-Host "  Creating $Type '$Name'..." -ForegroundColor Cyan
    $argList = @('create', $itemPath)
    if ($Params) {
        $pairs = $Params.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
        $argList += @('-P', ($pairs -join ','))
    }
    Invoke-Fab -FabArgs $argList | Out-Null
    return $itemPath
}

function Import-FabricItem {
    param(
        [Parameter(Mandatory)][string]$Workspace,
        [Parameter(Mandatory)][string]$Path        # local folder, e.g. items/ingest.Notebook
    )

    if (-not (Test-Path $Path)) {
        throw "Item definition folder not found: $Path"
    }
    $leaf       = Split-Path $Path -Leaf          # ingest.Notebook
    $remotePath = "$Workspace/$leaf"

    Write-Host "  Importing $leaf from $Path..." -ForegroundColor Cyan
    Invoke-Fab -FabArgs @('import', $remotePath, '-i', $Path, '-f') | Out-Null
    return $remotePath
}
