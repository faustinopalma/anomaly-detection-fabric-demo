"""Fix the eventstream destination kql_raw_telemetry: rebind to the real
kql_telemetry KQL database id and resume."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
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
db_name = os.environ["FABRIC_KQLDB_NAME"]

ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
es = next(e for e in s.get(f"{API}/workspaces/{ws}/eventstreams").json()["value"] if e["displayName"] == es_name)["id"]
db = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"] if d["displayName"] == db_name)
db_id = db["id"]
print(f"workspace={ws}  es={es}  kqlDb={db_name} ({db_id})")


def wait_lro(r: requests.Response) -> dict | None:
    if r.status_code != 202:
        return r.json() if r.content else None
    op = r.headers.get("Operation-Location") or r.headers.get("Location")
    if not op:
        return None
    for _ in range(120):
        time.sleep(3)
        body = s.get(op).json()
        status = (body.get("status") or "").lower()
        if status == "succeeded":
            # Fetch result
            result_url = op.rstrip("/") + "/result"
            rr = s.get(result_url)
            if rr.ok and rr.content:
                return rr.json()
            return body
        if status in ("failed", "cancelled"):
            raise SystemExit(f"Operation {status}: {body}")
    raise SystemExit(f"Operation did not finish: {op}")


# Fetch eventstream definition
r = s.post(f"{API}/workspaces/{ws}/items/{es}/getDefinition")
result = wait_lro(r)
print("DEBUG result keys:", list((result or {}).keys()))
print("DEBUG result:", json.dumps(result, indent=2)[:800])
parts = result["definition"]["parts"]

changed = False
for p in parts:
    if p["path"] == "eventstream.json":
        doc = json.loads(base64.b64decode(p["payload"]))
        for d in doc.get("destinations", []):
            if d.get("name") == "kql_raw_telemetry":
                props = d.setdefault("properties", {})
                old_item = props.get("itemId")
                old_ws = props.get("workspaceId")
                props["workspaceId"] = ws
                props["itemId"] = db_id
                props["databaseName"] = db_name
                props["tableName"] = "raw_telemetry"
                props["dataIngestionMode"] = "ProcessedIngestion"
                props.setdefault("inputSerialization", {"type": "Json", "properties": {"encoding": "UTF8"}})
                print(f"  rewiring destination: itemId {old_item} -> {db_id}  ws {old_ws} -> {ws}")
                changed = True
        if changed:
            p["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
            p["payloadType"] = "InlineBase64"

if not changed:
    print("  no change needed")
    sys.exit(0)

r = s.post(
    f"{API}/workspaces/{ws}/items/{es}/updateDefinition",
    json={"definition": {"parts": parts}},
)
wait_lro(r)
print("[ok] definition updated")

# Re-check status and resume if paused
topo = s.get(f"{API}/workspaces/{ws}/eventstreams/{es}/topology").json()
for d in topo.get("destinations", []):
    print(f"  DST {d['name']} status={d.get('status')} id={d.get('id')}")
    if d.get("status") == "Paused":
        url = f"{API}/workspaces/{ws}/eventstreams/{es}/destinations/{d['id']}/resume"
        rr = s.post(url, json={"startType": "Now"})
        print(f"    resume -> {rr.status_code} {rr.text[:200]}")
