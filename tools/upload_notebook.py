"""Upload a local `.ipynb` to Fabric as a Notebook item.

Creates the notebook if it does not exist, otherwise updates its
definition in place. Mirrors the auth pattern of the other `tools/*.py`
scripts (device-code credential cached on disk).

Usage:
    python tools/upload_notebook.py notebooks/04_train_univariate_ae.ipynb
    python tools/upload_notebook.py notebooks/04_train_univariate_ae.ipynb --name nb_train_export_onnx
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def _b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def _find_workspace_id(session: requests.Session, name: str) -> str:
    r = session.get(f"{API}/workspaces")
    r.raise_for_status()
    for w in r.json()["value"]:
        if w["displayName"] == name:
            return w["id"]
    raise SystemExit(f"workspace '{name}' not found")


def _find_notebook_id(session: requests.Session, ws_id: str, name: str) -> str | None:
    r = session.get(f"{API}/workspaces/{ws_id}/notebooks")
    r.raise_for_status()
    for n in r.json().get("value", []):
        if n["displayName"] == name:
            return n["id"]
    return None


def _wait_lro(session: requests.Session, response: requests.Response) -> None:
    """Poll a Fabric long-running operation (HTTP 202) until completion."""
    if response.status_code != 202:
        return
    op_url = response.headers.get("Location")
    if not op_url:
        return
    while True:
        r = session.get(op_url)
        r.raise_for_status()
        body = r.json() if r.content else {}
        status = (body.get("status") or "").lower()
        if status in ("succeeded", "completed"):
            return
        if status == "failed":
            raise SystemExit(f"operation failed: {body}")
        time.sleep(2)


def _build_definition(ipynb_path: Path, display_name: str) -> dict:
    ipynb_b64 = _b64(ipynb_path.read_bytes())
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Notebook", "displayName": display_name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    platform_b64 = _b64(json.dumps(platform, indent=2))
    return {
        "format": "ipynb",
        "parts": [
            {"path": "notebook-content.ipynb", "payload": ipynb_b64, "payloadType": "InlineBase64"},
            {"path": ".platform",              "payload": platform_b64, "payloadType": "InlineBase64"},
        ],
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Upload a .ipynb to Fabric as a Notebook item.")
    p.add_argument("notebook", type=Path, help="Path to local .ipynb file.")
    p.add_argument("--name", default=None,
                   help="Display name in Fabric (default: nb_<file stem>).")
    args = p.parse_args(argv)

    nb_path = args.notebook if args.notebook.is_absolute() else Path.cwd() / args.notebook
    if not nb_path.exists():
        raise SystemExit(f"file not found: {nb_path}")

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    tenant       = os.environ["FABRIC_TENANT_ID"]
    workspace    = os.environ["FABRIC_WORKSPACE_NAME"]
    display_name = args.name or f"nb_{nb_path.stem}"

    cred  = get_credential(tenant, FABRIC_SCOPE, repo_root)
    token = cred.get_token(FABRIC_SCOPE).token

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    })

    ws_id = _find_workspace_id(session, workspace)
    print(f"[ok]   workspace '{workspace}' -> {ws_id}")

    definition = _build_definition(nb_path, display_name)
    nb_id = _find_notebook_id(session, ws_id, display_name)

    if nb_id is None:
        print(f"[run]  creating notebook '{display_name}' from {nb_path.name}")
        body = {"displayName": display_name, "definition": definition}
        r = session.post(f"{API}/workspaces/{ws_id}/notebooks", json=body)
        if r.status_code not in (200, 201, 202):
            raise SystemExit(f"create failed {r.status_code}: {r.text}")
        _wait_lro(session, r)
        print(f"[ok]   created '{display_name}'")
    else:
        print(f"[run]  updating notebook '{display_name}' ({nb_id}) from {nb_path.name}")
        body = {"definition": definition}
        r = session.post(
            f"{API}/workspaces/{ws_id}/items/{nb_id}/updateDefinition",
            json=body,
        )
        if r.status_code not in (200, 202):
            raise SystemExit(f"update failed {r.status_code}: {r.text}")
        _wait_lro(session, r)
        print(f"[ok]   updated '{display_name}'")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
