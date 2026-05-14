"""Add a CustomEndpoint source named `sim_local` to the Fabric Eventstream
`es_machines` (if missing) and write its primary connection string into `.env`
as EVENTSTREAM_CONNECTION_STRING. No portal needed.

Usage:
    pip install -r tools/requirements-sim.txt
    python tools/01_setup_eventstream_source.py
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
SOURCE_NAME = "sim_local"
ENV_KEY = "EVENTSTREAM_CONNECTION_STRING"


def http(session: requests.Session, method: str, path: str, **kw) -> requests.Response:
    r = session.request(method, f"{API}{path}", **kw)
    if not r.ok and r.status_code != 202:
        raise SystemExit(f"{method} {path} -> HTTP {r.status_code}: {r.text}")
    return r


def wait_lro(session: requests.Session, r: requests.Response) -> None:
    """Poll Operation-Location if the response is 202; raise on failure."""
    if r.status_code != 202:
        return
    op_url = r.headers.get("Operation-Location") or r.headers.get("Location")
    if not op_url:
        return
    for _ in range(100):  # ~5 minutes at 3s
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


def ensure_custom_endpoint(session: requests.Session, ws: str, es: str) -> str:
    """Return the source id of a CustomEndpoint on the eventstream, creating it if missing."""
    topo = http(session, "GET", f"/workspaces/{ws}/eventstreams/{es}/topology").json()
    for s in topo.get("sources") or []:
        if s.get("type") == "CustomEndpoint":
            print(f"[ok]   reusing CustomEndpoint '{s['name']}' -> {s['id']}")
            return s["id"]

    print(f"[info] adding CustomEndpoint source '{SOURCE_NAME}'...")
    r = http(session, "POST", f"/workspaces/{ws}/items/{es}/getDefinition")
    wait_lro(session, r)
    parts = r.json()["definition"]["parts"]

    for p in parts:
        if p["path"] == "eventstream.json":
            doc = json.loads(base64.b64decode(p["payload"]))
            doc.setdefault("sources", []).append(
                {"name": SOURCE_NAME, "type": "CustomEndpoint", "properties": {}}
            )
            # Fabric requires a default stream when sources are present.
            streams = doc.setdefault("streams", [])
            default = next((s for s in streams if s.get("type") == "DefaultStream"), None)
            if default is None:
                streams.append({
                    "name": "default-stream",
                    "type": "DefaultStream",
                    "properties": {},
                    "inputNodes": [{"name": SOURCE_NAME}],
                })
            else:
                default.setdefault("inputNodes", []).append({"name": SOURCE_NAME})
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

    topo = http(session, "GET", f"/workspaces/{ws}/eventstreams/{es}/topology").json()
    for s in topo.get("sources") or []:
        if s.get("name") == SOURCE_NAME:
            print(f"[ok]   added source -> {s['id']}")
            return s["id"]
    raise SystemExit("Source was added but is not yet visible in the topology.")


def write_env(env_path: Path, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{ENV_KEY}="
    for i, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            lines[i] = prefix + value
            break
    else:
        lines.append(prefix + value)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    load_dotenv(env_path)

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    es_name = os.environ["FABRIC_EVENTSTREAM_NAME"]

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
    print(f"[ok]   workspace={ws}  eventstream={es}")

    source_id = ensure_custom_endpoint(session, ws, es)
    conn = http(
        session, "GET", f"/workspaces/{ws}/eventstreams/{es}/sources/{source_id}/connection"
    ).json()
    conn_str = conn["accessKeys"]["primaryConnectionString"]

    write_env(env_path, conn_str)
    print(f"[ok]   wrote {ENV_KEY} into {env_path}")
    print("Done. Run:  python simulator-local/simulate_machines.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
