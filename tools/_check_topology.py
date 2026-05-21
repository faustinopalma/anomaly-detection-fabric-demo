"""Check eventstream topology; auto-resume Paused destinations."""
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
s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
es_name = os.environ["FABRIC_EVENTSTREAM_NAME"]
ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
es = next(e for e in s.get(f"{API}/workspaces/{ws}/eventstreams").json()["value"] if e["displayName"] == es_name)["id"]
print(f"workspace={ws_name} ({ws})  eventstream={es_name} ({es})")

topo = s.get(f"{API}/workspaces/{ws}/eventstreams/{es}/topology").json()
for src in topo.get("sources", []):
    print(f"  SRC {src['name']}: {src.get('status')}")
for d in topo.get("destinations", []):
    print(f"  DST {d['name']}: {d.get('status')}  id={d.get('id')}")
    if d.get("status") == "Paused":
        url = f"{API}/workspaces/{ws}/eventstreams/{es}/destinations/{d['id']}/resume"
        r = s.post(url, json={"startType": "Now"})
        print(f"    resume -> {r.status_code} {r.text[:200]}")
