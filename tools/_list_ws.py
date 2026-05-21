"""List items in the workspace pointed to by .env."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

load_dotenv(override=True)
API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"

cred = get_credential(os.environ["FABRIC_TENANT_ID"], SCOPE, Path("."))
tok = cred.get_token(SCOPE).token
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {tok}"})

ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
wss = s.get(f"{API}/workspaces").json().get("value", [])
ws = next(w for w in wss if w["displayName"] == ws_name)
print(f"Workspace {ws_name} -> {ws['id']}")

items = s.get(f"{API}/workspaces/{ws['id']}/items").json().get("value", [])
for it in items:
    name = it.get("displayName") or ""
    typ = it.get("type") or ""
    print(f"  {typ:25s} {name:40s} {it.get('id')}")
