"""Shared auth helper: DeviceCodeCredential with persistent token cache.

First run prompts for device-code sign-in. The AuthenticationRecord is
saved to `.auth_record.json` and the refresh token is stored in the OS
secret store (DPAPI on Windows, Keychain on macOS, libsecret on Linux).
Subsequent runs reuse the cached token silently.
"""

from __future__ import annotations

from pathlib import Path

from azure.identity import (
    AuthenticationRecord,
    DeviceCodeCredential,
    TokenCachePersistenceOptions,
)

CACHE_NAME = "fabric-anomaly-detection"
RECORD_FILE = ".auth_record.json"


def get_credential(tenant: str, scope: str, repo_root: Path) -> DeviceCodeCredential:
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
        record = cred.authenticate(scopes=[scope])
        record_path.write_text(record.serialize())
        print(f"[auth] cached -> {record_path.name}")
    else:
        print("[auth] using cached credentials")

    return cred
