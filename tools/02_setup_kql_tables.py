"""Run one or more `.kql` scripts as control commands against the Fabric
KQL Database `kql_telemetry`. No portal needed.

Usage:
    pip install -r tools/requirements-sim.txt
    python tools/02_setup_kql_tables.py kql/01_tables.kql
    python tools/02_setup_kql_tables.py kql/01_tables.kql kql/02_models.kql
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def find_id(items: list[dict], name: str, kind: str) -> str:
    for it in items:
        if it.get("displayName") == name:
            return it["id"]
    raise SystemExit(f"{kind} '{name}' not found.")


def split_commands(text: str) -> list[str]:
    """Split a .kql file into individual control commands.

    Commands are separated by blank lines. Triple-backtick ``` blocks
    (used for multi-line literals like ingestion mappings) are kept whole.
    Lines starting with `//` are Kusto comments and are passed through.
    """
    commands: list[str] = []
    buf: list[str] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            buf.append(line)
            continue
        if not in_block and stripped == "":
            _flush(buf, commands)
            buf = []
        else:
            buf.append(line)
    _flush(buf, commands)
    return commands


def _flush(buf: list[str], commands: list[str]) -> None:
    if not buf:
        return
    # Drop leading comment-only / blank lines: Kusto admin commands must start
    # with '.' as the first non-whitespace character.
    while buf and (not buf[0].strip() or buf[0].lstrip().startswith("//")):
        buf.pop(0)
    cmd = "\n".join(buf).strip()
    if not cmd:
        return
    commands.append(cmd)


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: 02_setup_kql_tables.py <file.kql> [<file.kql> ...]", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]

    cred = get_credential(tenant, FABRIC_SCOPE, repo_root)
    fabric_token = cred.get_token(FABRIC_SCOPE).token

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {fabric_token}"})

    ws = find_id(session.get(f"{API}/workspaces").json()["value"], ws_name, "Workspace")
    db_id = find_id(
        session.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"],
        db_name,
        "KQL Database",
    )
    db_meta = session.get(f"{API}/workspaces/{ws}/kqlDatabases/{db_id}").json()
    query_uri = db_meta["properties"]["queryServiceUri"]
    print(f"[ok]   KQL DB '{db_name}' -> {query_uri}")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, cred)
    client = KustoClient(kcsb)

    for fname in argv:
        path = Path(fname)
        if not path.is_absolute():
            path = repo_root / path
        text = path.read_text(encoding="utf-8")
        cmds = split_commands(text)
        print(f"[run]  {path.name}: {len(cmds)} command(s)")
        for i, cmd in enumerate(cmds, 1):
            head = next((ln for ln in cmd.splitlines() if not ln.strip().startswith("//")), "")
            print(f"  [{i}/{len(cmds)}] {head[:90]} ...")
            try:
                client.execute_mgmt(db_name, cmd)
            except Exception:
                print(f"\n[FAIL] command {i}/{len(cmds)} from {path.name}:\n{'-' * 60}\n{cmd}\n{'-' * 60}", file=sys.stderr)
                raise
        print(f"[ok]   {path.name} done")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
