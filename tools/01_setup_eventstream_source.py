"""Automate the creation of a Custom Endpoint source on the Fabric Eventstream
`es_machines` and write its Event Hubs-compatible primary connection string
into `.env` (as EVENTSTREAM_CONNECTION_STRING).

What this script does
---------------------
1. Authenticates against Microsoft Entra via device code, for the Fabric scope
   (https://api.fabric.microsoft.com/.default).
2. Resolves the workspace ID from FABRIC_WORKSPACE_NAME.
3. Resolves the eventstream item ID from FABRIC_EVENTSTREAM_NAME.
4. Calls the Eventstream Topology REST API. If no `CustomEndpoint` source
   exists, it adds one called `sim_local` via Update Item Definition.
5. Calls `GET .../sources/{sourceId}/connection` to retrieve the primary
   connection string and writes/updates the EVENTSTREAM_CONNECTION_STRING
   line in `.env`.

Prerequisites
-------------
    pip install -r tools/requirements-sim.txt

Usage
-----
    python tools/setup_eventstream_endpoint.py            # adds + writes .env
    python tools/setup_eventstream_endpoint.py --dry-run  # only prints
    python tools/setup_eventstream_endpoint.py --source-name my_app
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from azure.identity import DeviceCodeCredential
from dotenv import load_dotenv


FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
LRO_POLL_INTERVAL_S = 3
LRO_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class Fabric:
    def __init__(self, token: str) -> None:
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def get(self, path: str, **kw) -> requests.Response:
        return self.s.get(f"{FABRIC_API}{path}", **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.s.post(f"{FABRIC_API}{path}", **kw)

    def wait_lro(self, response: requests.Response) -> requests.Response:
        """Poll an Operation-Location header until terminal state."""
        if response.status_code != 202:
            return response
        op_url = response.headers.get("Operation-Location") or response.headers.get("Location")
        if not op_url:
            return response
        deadline = time.time() + LRO_TIMEOUT_S
        while time.time() < deadline:
            r = self.s.get(op_url)
            r.raise_for_status()
            body = r.json()
            status = body.get("status", "").lower()
            if status in ("succeeded", "failed", "cancelled"):
                if status != "succeeded":
                    raise RuntimeError(f"LRO ended with status={status}: {body}")
                return r
            time.sleep(LRO_POLL_INTERVAL_S)
        raise TimeoutError(f"LRO did not finish within {LRO_TIMEOUT_S}s: {op_url}")


def _raise_for(r: requests.Response, what: str) -> None:
    if not r.ok:
        raise RuntimeError(f"{what} failed: HTTP {r.status_code} {r.text}")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_workspace_id(fab: Fabric, name: str) -> str:
    r = fab.get("/workspaces")
    _raise_for(r, "list workspaces")
    for ws in r.json().get("value", []):
        if ws.get("displayName") == name:
            return ws["id"]
    raise SystemExit(f"Workspace '{name}' not found.")


def find_eventstream_id(fab: Fabric, workspace_id: str, name: str) -> str:
    r = fab.get(f"/workspaces/{workspace_id}/eventstreams")
    _raise_for(r, "list eventstreams")
    for es in r.json().get("value", []):
        if es.get("displayName") == name:
            return es["id"]
    raise SystemExit(f"Eventstream '{name}' not found in workspace.")


# ---------------------------------------------------------------------------
# Topology read / update
# ---------------------------------------------------------------------------

def get_topology(fab: Fabric, workspace_id: str, eventstream_id: str) -> dict[str, Any]:
    r = fab.get(f"/workspaces/{workspace_id}/eventstreams/{eventstream_id}/topology")
    _raise_for(r, "get topology")
    return r.json()


def find_custom_endpoint(topology: dict[str, Any], preferred_name: str) -> dict[str, Any] | None:
    sources = topology.get("sources") or []
    # Prefer one matching the requested name; else any CustomEndpoint.
    for s in sources:
        if s.get("type") == "CustomEndpoint" and s.get("name") == preferred_name:
            return s
    for s in sources:
        if s.get("type") == "CustomEndpoint":
            return s
    return None


def get_definition(fab: Fabric, workspace_id: str, item_id: str) -> dict[str, Any]:
    """POST .../items/{id}/getDefinition?format=Default returns base64 parts."""
    r = fab.post(f"/workspaces/{workspace_id}/items/{item_id}/getDefinition")
    r = fab.wait_lro(r)
    _raise_for(r, "get item definition")
    return r.json()


def update_definition(fab: Fabric, workspace_id: str, item_id: str, definition: dict[str, Any]) -> None:
    r = fab.post(
        f"/workspaces/{workspace_id}/items/{item_id}/updateDefinition",
        json={"definition": definition},
    )
    r = fab.wait_lro(r)
    _raise_for(r, "update item definition")


def add_custom_endpoint_to_definition(definition: dict[str, Any], source_name: str) -> dict[str, Any]:
    """Return a new definition with a CustomEndpoint source added to eventstream.json."""
    parts = definition.get("definition", {}).get("parts") or definition.get("parts") or []
    new_parts: list[dict[str, Any]] = []
    found_es_json = False
    for part in parts:
        if part.get("path") == "eventstream.json":
            found_es_json = True
            decoded = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
            decoded.setdefault("sources", [])
            decoded.setdefault("destinations", [])
            decoded.setdefault("streams", [])
            decoded.setdefault("operators", [])
            # Append the new source. Fabric will assign an id on publish.
            decoded["sources"].append({
                "name": source_name,
                "type": "CustomEndpoint",
                "properties": {},
            })
            # Ensure there's a default stream wired to the new source so the
            # topology stays consistent. If a default stream already exists
            # we just append the new source as an inputNode.
            default_stream = next(
                (s for s in decoded["streams"] if s.get("type") == "DefaultStream"),
                None,
            )
            if default_stream is None:
                decoded["streams"].append({
                    "name": "default-stream",
                    "type": "DefaultStream",
                    "properties": {},
                    "inputNodes": [{"name": source_name}],
                })
            else:
                default_stream.setdefault("inputNodes", []).append({"name": source_name})
            new_payload = base64.b64encode(
                json.dumps(decoded, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            new_parts.append({
                "path": "eventstream.json",
                "payload": new_payload,
                "payloadType": "InlineBase64",
            })
        else:
            new_parts.append(part)
    if not found_es_json:
        # Brand-new empty eventstream: create the file from scratch.
        decoded = {
            "sources": [{"name": source_name, "type": "CustomEndpoint", "properties": {}}],
            "destinations": [],
            "streams": [{
                "name": "default-stream",
                "type": "DefaultStream",
                "properties": {},
                "inputNodes": [{"name": source_name}],
            }],
            "operators": [],
            "compatibilityLevel": "1.0",
        }
        new_parts.append({
            "path": "eventstream.json",
            "payload": base64.b64encode(json.dumps(decoded).encode("utf-8")).decode("ascii"),
            "payloadType": "InlineBase64",
        })
    return {"parts": new_parts}


# ---------------------------------------------------------------------------
# Source connection
# ---------------------------------------------------------------------------

def get_source_connection(fab: Fabric, workspace_id: str, eventstream_id: str, source_id: str) -> dict[str, Any]:
    r = fab.get(
        f"/workspaces/{workspace_id}/eventstreams/{eventstream_id}/sources/{source_id}/connection"
    )
    _raise_for(r, "get source connection")
    return r.json()


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

ENV_KEY = "EVENTSTREAM_CONNECTION_STRING"


def write_env(env_path: Path, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pattern = re.compile(rf"^\s*{ENV_KEY}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{ENV_KEY}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{ENV_KEY}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-name", default="sim_local",
                   help="Name to use for the CustomEndpoint source (default: sim_local).")
    p.add_argument("--dry-run", action="store_true",
                   help="Do not modify the eventstream or .env. Just print the actions.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    load_dotenv(env_path)

    tenant_id        = os.environ["FABRIC_TENANT_ID"]
    workspace_name   = os.environ["FABRIC_WORKSPACE_NAME"]
    eventstream_name = os.environ["FABRIC_EVENTSTREAM_NAME"]

    print(f"[auth] device-code sign-in (tenant={tenant_id})...")
    cred = DeviceCodeCredential(tenant_id=tenant_id)
    token = cred.get_token(FABRIC_SCOPE).token
    fab = Fabric(token)

    workspace_id = find_workspace_id(fab, workspace_name)
    print(f"[ok]   workspace '{workspace_name}' -> {workspace_id}")

    eventstream_id = find_eventstream_id(fab, workspace_id, eventstream_name)
    print(f"[ok]   eventstream '{eventstream_name}' -> {eventstream_id}")

    topology = get_topology(fab, workspace_id, eventstream_id)
    custom = find_custom_endpoint(topology, args.source_name)

    if custom is None:
        print(f"[info] no CustomEndpoint source found - adding '{args.source_name}'.")
        if args.dry_run:
            print("[dry]  would update eventstream definition; stopping here.")
            return 0
        definition = get_definition(fab, workspace_id, eventstream_id)
        new_def = add_custom_endpoint_to_definition(definition, args.source_name)
        update_definition(fab, workspace_id, eventstream_id, new_def)
        # Re-read topology to get the auto-assigned source ID.
        topology = get_topology(fab, workspace_id, eventstream_id)
        custom = find_custom_endpoint(topology, args.source_name)
        if custom is None:
            raise SystemExit("Custom endpoint was added but is not yet visible in the topology.")
        print(f"[ok]   added CustomEndpoint source -> {custom['id']}")
    else:
        print(f"[ok]   reusing existing CustomEndpoint '{custom.get('name')}' -> {custom['id']}")

    conn_info = get_source_connection(fab, workspace_id, eventstream_id, custom["id"])
    conn_str  = conn_info["accessKeys"]["primaryConnectionString"]

    masked = conn_str[:60] + "...<redacted>"
    print(f"[ok]   primary connection string: {masked}")

    if args.dry_run:
        print("[dry]  would write EVENTSTREAM_CONNECTION_STRING into .env")
        return 0

    write_env(env_path, conn_str)
    print(f"[ok]   wrote {ENV_KEY} into {env_path}")
    print("Done. You can now run:  python tools/simulate_machines.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
