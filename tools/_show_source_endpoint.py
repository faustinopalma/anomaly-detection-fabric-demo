"""Dump eventstream source endpoint + connection string for the current sim_local source."""
from __future__ import annotations
import base64
import json
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
s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
es_name = os.environ["FABRIC_EVENTSTREAM_NAME"]
ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
es = next(e for e in s.get(f"{API}/workspaces/{ws}/eventstreams").json()["value"] if e["displayName"] == es_name)["id"]
print(f"workspace={ws_name} ({ws})  eventstream={es_name} ({es})")

# Get definition (base64) and decode
r = s.post(f"{API}/workspaces/{ws}/eventstreams/{es}/getDefinition")
if r.status_code in (200, 202):
    # LRO if 202
    if r.status_code == 202:
        loc = r.headers.get("Location") or r.headers.get("x-ms-operation-id")
        import time
        op_id = r.headers["x-ms-operation-id"]
        for _ in range(30):
            op = s.get(f"{API}/operations/{op_id}").json()
            if op.get("status") in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        res = s.get(f"{API}/operations/{op_id}/result").json()
    else:
        res = r.json()
    for part in res.get("definition", {}).get("parts", []):
        if part["path"].endswith("eventstream.json"):
            data = json.loads(base64.b64decode(part["payload"]).decode())
            for src in data.get("sources", []):
                print(f"\nSOURCE name={src.get('name')} type={src.get('type')}")
                print(json.dumps(src.get("properties", {}), indent=2))
