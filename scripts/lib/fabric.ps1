# Thin wrappers around the Fabric CLI (`fab`) that make scripts idempotent
# and easier to read. All functions write progress to the host and throw on
# unexpected failures.

$script:FabExe = $null

function Resolve-FabCli {
    if ($script:FabExe) {
        return $script:FabExe
    }

    $fabCmd = Get-Command fab -ErrorAction SilentlyContinue
    if ($fabCmd) {
        $script:FabExe = $fabCmd.Source
        return $script:FabExe
    }

    $repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $venvFab  = Join-Path $repoRoot '.venv\Scripts\fab.exe'
    if (Test-Path $venvFab) {
        $script:FabExe = $venvFab
        return $script:FabExe
    }

    return $null
}

function Assert-FabCli {
    if (-not (Resolve-FabCli)) {
        throw "Fabric CLI not found. Install with: pip install --upgrade ms-fabric-cli"
    }
}

function Invoke-Fab {
    # Runs `fab` and throws if exit code is non-zero. Returns stdout lines.
    # Arguments must be passed as a single array to avoid PowerShell's
    # automatic parameter binding (e.g. -P clashing with -PipelineVariable).
    param([Parameter(Mandatory)][string[]]$FabArgs)

    $fabExe = Resolve-FabCli
    if (-not $fabExe) {
        throw "Fabric CLI not found. Install with: pip install --upgrade ms-fabric-cli"
    }

    Write-Host "  > fab $($FabArgs -join ' ')" -ForegroundColor DarkGray
    $prevPyIo = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $output = & $fabExe @FabArgs 2>&1
    } finally {
        $env:PYTHONIOENCODING = $prevPyIo
    }
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
    $fabExe = Resolve-FabCli
    if (-not $fabExe) {
        throw "Fabric CLI not found. Install with: pip install --upgrade ms-fabric-cli"
    }
    $prevPyIo = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $out = & $fabExe exists $Path 2>&1 | Out-String
    } finally {
        $env:PYTHONIOENCODING = $prevPyIo
    }
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

function Get-FabricItemId {
    # Returns the GUID of an existing Fabric item, or throws if not found.
    # Uses `fab get <path> -q id`, which prints the raw value.
    param(
        [Parameter(Mandatory)][string]$Workspace,   # e.g. /my-ws.Workspace
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Type         # e.g. Eventhouse
    )

    $itemName = "$Name.$Type"
    $out = Invoke-Fab -FabArgs @('ls', '-l', $Workspace) | Out-String
    $id = ($out -split "`r?`n" |
        ForEach-Object {
            if ($_ -match '^\s*(\S+)\s+([0-9a-fA-F-]{36})\s*$') {
                [pscustomobject]@{ Name = $Matches[1]; Id = $Matches[2] }
            }
        } |
        Where-Object { $_ -and $_.Name -eq $itemName } |
        Select-Object -ExpandProperty Id -First 1)

    if (-not $id) {
        throw "Could not extract item id for $Workspace/$itemName. Raw output:`n$out"
    }
    return $id
}

function New-FabricKQLDatabase {
    <#
    .SYNOPSIS        Creates a KQLDatabase inside an existing Eventhouse via direct REST API.
    .DESCRIPTION
        `fab create -P parentEventhouseItemId=<id>` puts the parameter at the
        top level of the request body, but the Fabric REST API requires it inside
        a `creationPayload` object. Without the correct payload, Fabric silently
        creates an auto-Eventhouse (<dbname>_auto) and places the DB there.
        This function bypasses `fab create` and calls the REST API directly.
    #>
    param(
        [Parameter(Mandatory)][string]$Workspace,          # e.g. /my-ws.Workspace
        [Parameter(Mandatory)][string]$Name,               # display name
        [Parameter(Mandatory)][string]$ParentEventhouseId  # GUID of the parent Eventhouse
    )

    $itemPath = "$Workspace/$Name.KQLDatabase"
    if (Test-FabPath $itemPath) {
        Write-Host "  KQLDatabase '$Name' already exists - skipping." -ForegroundColor Yellow
        return $itemPath
    }

    # Resolve workspace GUID
    $wsId = (Invoke-Fab -FabArgs @('get', $Workspace, '-q', 'id') |
             Where-Object { $_ -match '^[0-9a-f-]{36}$' } |
             Select-Object -First 1)
    if (-not $wsId) {
        throw "Could not retrieve workspace ID for $Workspace"
    }
    $wsId = $wsId.Trim()

    Write-Host "  Creating KQLDatabase '$Name' inside Eventhouse $ParentEventhouseId..." -ForegroundColor Cyan

    # Obtain access token via Azure CLI (silent, no browser/device-code prompt)
    $tokenJson   = az account get-access-token --resource https://api.fabric.microsoft.com 2>&1 | ConvertFrom-Json
    $bearerToken = $tokenJson.accessToken

    $headers = @{
        Authorization  = "Bearer $bearerToken"
        'Content-Type' = 'application/json'
    }
    $body = @{
        displayName     = $Name
        type            = 'KQLDatabase'
        creationPayload = @{
            databaseType           = 'ReadWrite'
            parentEventhouseItemId = $ParentEventhouseId
        }
    } | ConvertTo-Json -Depth 5 -Compress

    $resp = Invoke-WebRequest `
        -Uri            "https://api.fabric.microsoft.com/v1/workspaces/$wsId/kqlDatabases" `
        -Method         POST `
        -Headers        $headers `
        -Body           $body `
        -SkipHttpErrorCheck

    if ($resp.StatusCode -eq 201) {
        Write-Host "  KQLDatabase '$Name' created." -ForegroundColor Green
    } elseif ($resp.StatusCode -eq 202) {
        # Long-running operation: poll the Location URL until complete
        $opUrl = ([string]($resp.Headers['Location'])).Trim()
        Write-Host "  Waiting for KQLDatabase '$Name' creation (async)..." -ForegroundColor DarkGray
        $maxRetries = 30
        for ($i = 0; $i -lt $maxRetries; $i++) {
            Start-Sleep -Seconds 5
            $status = Invoke-RestMethod -Uri $opUrl -Headers $headers
            $state  = $status.status
            if ($state -eq 'Succeeded') {
                Write-Host "  KQLDatabase '$Name' created." -ForegroundColor Green
                break
            } elseif ($state -in 'Failed', 'Canceled') {
                throw "KQLDatabase creation failed: $($status | ConvertTo-Json -Compress)"
            }
            Write-Host "  ... status=$state ($($i+1)/$maxRetries)" -ForegroundColor DarkGray
        }
        if ($i -ge $maxRetries) {
            throw "Timed out waiting for KQLDatabase '$Name' creation."
        }
    } else {
        throw "Unexpected status $($resp.StatusCode) creating KQLDatabase '$Name': $($resp.Content)"
    }

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
