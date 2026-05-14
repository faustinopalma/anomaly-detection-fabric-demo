"""Add an Eventhouse (push-mode) destination to the Fabric Eventstream
`es_machines`, wired to KQL DB `kql_telemetry` -> table `raw_telemetry`.

Uses ProcessedIngestion mode: the eventstream operator parses incoming JSON
and writes columns into the destination table by name. No pre-existing
data-stream connection on the Eventhouse is required.

Idempotent: skips if a destination with the same name already exists.

Usage:
    pip install -r tools/requirements-sim.txt
    python tools/03_setup_eventstream_destination.py
"""

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

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
DEST_NAME = "kql_raw_telemetry"
TABLE_NAME = "raw_telemetry"


def http(session: requests.Session, method: str, path: str, **kw) -> requests.Response:
    r = session.request(method, f"{API}{path}", **kw)
    if not r.ok and r.status_code != 202:
        raise SystemExit(f"{method} {path} -> HTTP {r.status_code}: {r.text}")
    return r


def wait_lro(session: requests.Session, r: requests.Response) -> None:
    if r.status_code != 202:
        return
    op_url = r.headers.get("Operation-Location") or r.headers.get("Location")
    if not op_url:
        return
    for _ in range(100):
        time.sleep(3)
        body = session.get(op_url).json()
        status = (body.get("status") or "").lower()
        if status == "succeeded":
            return
        if status in ("failed", "cancelled"):
            raise SystemExit(f"Operation {status}: {body}")
    raise SystemExit(f"Operation did not finish within 5 min: {op_url}")


def find_id(items: list[dict], name: str, kind: str) -> str:
    for it in items:
        if it.get("displayName") == name:
            return it["id"]
    raise SystemExit(f"{kind} '{name}' not found.")


def ensure_destination(
    session: requests.Session, ws: str, es: str, db_id: str, db_name: str
) -> None:
    topo = http(session, "GET", f"/workspaces/{ws}/eventstreams/{es}/topology").json()

    for d in topo.get("destinations") or []:
        if d.get("name") == DEST_NAME:
            print(f"[ok]   destination '{DEST_NAME}' already exists")
            return

    default_stream = next(
        (s for s in topo.get("streams") or [] if s.get("type") == "DefaultStream"),
        None,
    )
    if not default_stream:
        raise SystemExit("No DefaultStream found. Run 01_setup_eventstream_source.py first.")
    stream_name = default_stream["name"]

    print(f"[info] adding Eventhouse destination '{DEST_NAME}' -> {db_name}.{TABLE_NAME}")
    r = http(session, "POST", f"/workspaces/{ws}/items/{es}/getDefinition")
    wait_lro(session, r)
    parts = r.json()["definition"]["parts"]

    for p in parts:
        if p["path"] == "eventstream.json":
            doc = json.loads(base64.b64decode(p["payload"]))
            doc.setdefault("destinations", []).append({
                "name": DEST_NAME,
                "type": "Eventhouse",
                "properties": {
                    "dataIngestionMode": "ProcessedIngestion",
                    "workspaceId": ws,
                    "itemId": db_id,
                    "databaseName": db_name,
                    "tableName": TABLE_NAME,
                    "inputSerialization": {
                        "type": "Json",
                        "properties": {"encoding": "UTF8"},
                    },
                },
                "inputNodes": [{"name": stream_name}],
            })
            p["payload"] = base64.b64encode(json.dumps(doc).encode()).decode()
            p["payloadType"] = "InlineBase64"
            break
    else:
        raise SystemExit("eventstream.json not found in item definition.")

    r = http(
        session,
        "POST",
        f"/workspaces/{ws}/items/{es}/updateDefinition",
        json={"definition": {"parts": parts}},
    )
    wait_lro(session, r)
    print(f"[ok]   destination added")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    load_dotenv(env_path)

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    es_name = os.environ["FABRIC_EVENTSTREAM_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]

    cred = get_credential(tenant, SCOPE, repo_root)
    token = cred.get_token(SCOPE).token
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    ws = find_id(http(session, "GET", "/workspaces").json().get("value", []), ws_name, "Workspace")
    es = find_id(
        http(session, "GET", f"/workspaces/{ws}/eventstreams").json().get("value", []),
        es_name,
        "Eventstream",
    )
    db_id = find_id(
        http(session, "GET", f"/workspaces/{ws}/kqlDatabases").json().get("value", []),
        db_name,
        "KQL Database",
    )
    print(f"[ok]   workspace={ws}  eventstream={es}  database={db_name} ({db_id})")

    ensure_destination(session, ws, es, db_id, db_name)
    print("Done. Now run:  python simulator-local/simulate_machines.py")
    print("Then verify:    raw_telemetry | count")
    return 0


if __name__ == "__main__":
    sys.exit(main())
