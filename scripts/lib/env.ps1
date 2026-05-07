# Loads KEY=VALUE pairs from a .env file into the current process environment.
# Lines beginning with `#` and blank lines are ignored. Values may be quoted.

function Import-DotEnv {
    [CmdletBinding()]
    param(
        [string]$Path = (Join-Path $PSScriptRoot '..' '..' '.env')
    )

    if (-not (Test-Path $Path)) {
        throw ".env file not found at '$Path'. Copy .env.example to .env and fill it in."
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }

        if ($line -match '^\s*([^=\s]+)\s*=\s*(.*)\s*$') {
            $key   = $Matches[1]
            $value = $Matches[2]
            # Strip surrounding single or double quotes
            if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") {
                $value = $Matches[1]
            }
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
}

function Assert-EnvVars {
    param([string[]]$Names)

    $missing = @()
    foreach ($n in $Names) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($n))) {
            $missing += $n
        }
    }
    if ($missing.Count -gt 0) {
        throw "Missing required environment variables: $($missing -join ', ')"
    }
}
