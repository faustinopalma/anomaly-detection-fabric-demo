<#
.SYNOPSIS
    Create / configure (idempotently) the Entra ID app registration that gates
    the simulator control panel behind sign-in. Safe to run repeatedly.

.DESCRIPTION
    Single-tenant SPA app registration. This script:
      1. Finds the app by display name (creates it if missing)
      2. Sets signInAudience = AzureADMyOrg, exposes the `access_as_user`
         delegated scope, identifierUri api://{clientId}, token v2
      3. Sets SPA redirect URIs (the Static Web App URL + localhost:4280)
      4. Pre-authorizes the Azure CLI app (for token-based smoke tests)
      5. Ensures the service principal exists with
         appRoleAssignmentRequired = true
      6. Assigns the signed-in admin user (so only assigned users can sign in)
      7. Grants tenant-wide admin consent for the scope
      8. Writes SIM_AUTH_TENANT_ID / SIM_AUTH_CLIENT_ID back to .env and prints
         the client id + scope (consumed by the container + webapp deploys)

    Every Graph call is idempotent: existing objects are reused, existing
    assignments / grants are detected and skipped.

.EXAMPLE
    pwsh ./scripts/setup-app-registration.ps1 -SpaRedirectUris @(
        "https://jolly-pebble-0d6f26703.7.azurestaticapps.net",
        "http://localhost:4280")

.OUTPUTS
    PSCustomObject with ClientId, ObjectId, ServicePrincipalId, Scope.
#>
[CmdletBinding()]
param(
    [string]   $DisplayName = "Anomaly Sim Control Panel",
    [string[]] $SpaRedirectUris = @("http://localhost:4280"),
    [string]   $TenantId,
    [switch]   $SkipUserAssignment
)

$ErrorActionPreference = "Stop"

# Stable identifier for the delegated scope (kept constant across runs so the
# api://.../access_as_user value never changes).
$SCOPE_ID    = "b6f3c2a1-7d44-4e6b-9a21-0f8e5c2d1a90"
$SCOPE_VALUE = "access_as_user"
$AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
$GRAPH = "https://graph.microsoft.com/v1.0"

# ---------------------------------------------------------------------------
# 0. Load .env (for FABRIC_TENANT_ID) and ensure az login on the right tenant
# ---------------------------------------------------------------------------
. (Join-Path $PSScriptRoot "lib/env.ps1")
Import-DotEnv
if (-not $TenantId) { $TenantId = $env:FABRIC_TENANT_ID }
if (-not $TenantId) { throw "TenantId not provided and FABRIC_TENANT_ID missing in .env" }

$ctx = az account show 2>$null | ConvertFrom-Json
if (-not $ctx -or $ctx.tenantId -ne $TenantId) {
    Write-Host "[appreg] az login --use-device-code --tenant $TenantId" -ForegroundColor Cyan
    az login --use-device-code --tenant $TenantId --allow-no-subscriptions | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "az login failed" }
}

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [object]$Body
    )
    $args = @("rest", "--method", $Method, "--url", $Url,
              "--headers", "Content-Type=application/json")
    if ($null -ne $Body) {
        $tmp = New-TemporaryFile
        ($Body | ConvertTo-Json -Depth 20) | Set-Content -LiteralPath $tmp -Encoding utf8
        $args += @("--body", "@$tmp")
    }
    $out = az @args 2>&1
    $code = $LASTEXITCODE
    if ($tmp) { Remove-Item $tmp -ErrorAction SilentlyContinue }
    if ($code -ne 0) { throw "Graph $Method $Url failed: $out" }
    if ($out) { return ($out | ConvertFrom-Json) }
    return $null
}

# ---------------------------------------------------------------------------
# 1. Find or create the application
# ---------------------------------------------------------------------------
Write-Host "[appreg] looking up application '$DisplayName'" -ForegroundColor Cyan
$existing = (Invoke-Graph GET "$GRAPH/applications?`$filter=displayName eq '$DisplayName'").value
if ($existing -and $existing.Count -gt 0) {
    $app = $existing[0]
    Write-Host "[appreg] reusing existing app (appId $($app.appId))" -ForegroundColor Green
} else {
    Write-Host "[appreg] creating application" -ForegroundColor Cyan
    $app = Invoke-Graph POST "$GRAPH/applications" @{
        displayName    = $DisplayName
        signInAudience = "AzureADMyOrg"
    }
    Write-Host "[appreg] created app (appId $($app.appId))" -ForegroundColor Green
}
$objectId = $app.id
$clientId = $app.appId

# ---------------------------------------------------------------------------
# 2. Configure API (scope + token v2 + pre-authorized Azure CLI) and SPA URIs
# ---------------------------------------------------------------------------
Write-Host "[appreg] configuring API surface + SPA redirect URIs" -ForegroundColor Cyan
$apiPatch = @{
    identifierUris = @("api://$clientId")
    signInAudience = "AzureADMyOrg"
    spa = @{ redirectUris = $SpaRedirectUris }
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(@{
            id    = $SCOPE_ID
            value = $SCOPE_VALUE
            type  = "User"
            isEnabled = $true
            adminConsentDisplayName = "Access simulator control API"
            adminConsentDescription = "Allow the app to access the simulator control API as the signed-in user."
            userConsentDisplayName  = "Access simulator control API"
            userConsentDescription  = "Allow the app to access the simulator control API on your behalf."
        })
        preAuthorizedApplications = @(@{
            appId = $AZURE_CLI_APP_ID
            delegatedPermissionIds = @($SCOPE_ID)
        })
    }
}
Invoke-Graph PATCH "$GRAPH/applications/$objectId" $apiPatch | Out-Null

# ---------------------------------------------------------------------------
# 3. Ensure the service principal + appRoleAssignmentRequired
# ---------------------------------------------------------------------------
$sp = (Invoke-Graph GET "$GRAPH/servicePrincipals?`$filter=appId eq '$clientId'").value | Select-Object -First 1
if (-not $sp) {
    Write-Host "[appreg] creating service principal" -ForegroundColor Cyan
    $sp = Invoke-Graph POST "$GRAPH/servicePrincipals" @{ appId = $clientId }
}
$spId = $sp.id
if (-not $sp.appRoleAssignmentRequired) {
    Invoke-Graph PATCH "$GRAPH/servicePrincipals/$spId" @{ appRoleAssignmentRequired = $true } | Out-Null
}
Write-Host "[appreg] service principal $spId (assignment required)" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4. Assign the signed-in user (default access role) — idempotent
# ---------------------------------------------------------------------------
if (-not $SkipUserAssignment) {
    $me = Invoke-Graph GET "$GRAPH/me"
    $myId = $me.id
    $assignments = (Invoke-Graph GET "$GRAPH/servicePrincipals/$spId/appRoleAssignedTo").value
    $already = $assignments | Where-Object { $_.principalId -eq $myId }
    if ($already) {
        Write-Host "[appreg] user $($me.userPrincipalName) already assigned" -ForegroundColor Green
    } else {
        Invoke-Graph POST "$GRAPH/servicePrincipals/$spId/appRoleAssignedTo" @{
            principalId = $myId
            resourceId  = $spId
            appRoleId   = "00000000-0000-0000-0000-000000000000"
        } | Out-Null
        Write-Host "[appreg] assigned user $($me.userPrincipalName)" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------------------
# 5. Grant tenant-wide admin consent for the scope — idempotent
# ---------------------------------------------------------------------------
$grants = (Invoke-Graph GET "$GRAPH/oauth2PermissionGrants?`$filter=clientId eq '$spId'").value
$hasGrant = $grants | Where-Object { $_.resourceId -eq $spId -and $_.scope -match $SCOPE_VALUE }
if ($hasGrant) {
    Write-Host "[appreg] admin consent already granted" -ForegroundColor Green
} else {
    Invoke-Graph POST "$GRAPH/oauth2PermissionGrants" @{
        clientId    = $spId
        consentType = "AllPrincipals"
        resourceId  = $spId
        scope       = $SCOPE_VALUE
    } | Out-Null
    Write-Host "[appreg] granted admin consent" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 6. Persist results to .env (SIM_AUTH_*) and return them
# ---------------------------------------------------------------------------
$scope = "api://$clientId/$SCOPE_VALUE"
$envFile = (Resolve-Path (Join-Path $PSScriptRoot "../.env")).Path
function Set-EnvLine([string]$key, [string]$value) {
    $lines = Get-Content -LiteralPath $envFile
    if ($lines -match "^$key=") {
        $lines = $lines -replace "^$key=.*", "$key=$value"
    } else {
        $lines += "$key=$value"
    }
    Set-Content -LiteralPath $envFile -Value $lines -Encoding utf8
}
Set-EnvLine "SIM_AUTH_TENANT_ID" $TenantId
Set-EnvLine "SIM_AUTH_CLIENT_ID" $clientId

Write-Host ""
Write-Host "[appreg] done." -ForegroundColor Green
Write-Host "  clientId: $clientId" -ForegroundColor Yellow
Write-Host "  scope:    $scope" -ForegroundColor Yellow
Write-Host "  redirect: $($SpaRedirectUris -join ', ')" -ForegroundColor Yellow

[pscustomobject]@{
    ClientId            = $clientId
    ObjectId            = $objectId
    ServicePrincipalId  = $spId
    TenantId            = $TenantId
    Scope               = $scope
}
