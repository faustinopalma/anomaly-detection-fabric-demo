"""Shared auth helper for Fabric REST/Kusto scripts.

Auth strategy:
1) Try Azure CLI cached login first (silent on dev machines).
2) Fall back to Device Code with persistent token cache.

The AuthenticationRecord is saved to `.auth_record.json` and the refresh
token is stored in the OS secret store (DPAPI on Windows, Keychain on
macOS, libsecret on Linux).
"""

from __future__ import annotations

from pathlib import Path

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import (
    AuthenticationRecord,
    AzureCliCredential,
    DeviceCodeCredential,
    TokenCachePersistenceOptions,
)

CACHE_NAME = "fabric-anomaly-detection"
RECORD_FILE = ".auth_record.json"


def get_credential(tenant: str, scope: str, repo_root: Path) -> TokenCredential:
    # Fast path: reuse existing `az login` context if available.
    try:
        cli_cred = AzureCliCredential(tenant_id=tenant)
        cli_cred.get_token(scope)
        print("[auth] using Azure CLI cached credentials")
        return cli_cred
    except Exception:
        # Fall back to device-code auth below.
        pass

    record_path = repo_root / RECORD_FILE
    cache_opts = TokenCachePersistenceOptions(name=CACHE_NAME)

    record = None
    if record_path.exists():
        try:
            record = AuthenticationRecord.deserialize(record_path.read_text())
        except Exception:
            record = None

    cred = DeviceCodeCredential(
        tenant_id=tenant,
        cache_persistence_options=cache_opts,
        authentication_record=record,
    )

    if record is None:
        print("[auth] device-code sign-in (first run, will be cached)...")
        try:
            record = cred.authenticate(scopes=[scope])
        except ClientAuthenticationError as exc:
            raise SystemExit(str(exc)) from exc
        record_path.write_text(record.serialize())
        print(f"[auth] cached -> {record_path.name}")
    else:
        print("[auth] using cached credentials")

    return cred
