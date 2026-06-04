"""Inspect the Fabric `anomalies` table: recency, schema, sensor_id values."""

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
SCOPE = "https://api.fabric.microsoft.com/.default"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db = os.environ["FABRIC_KQLDB_NAME"]
    cred = get_credential(tenant, SCOPE, root)
    tok = cred.get_token(SCOPE).token
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}"})
    wsid = next(w["id"] for w in s.get(f"{API}/workspaces").json()["value"]
                if w["displayName"] == ws_name)
    dbid = next(d["id"] for d in s.get(f"{API}/workspaces/{wsid}/kqlDatabases").json()["value"]
                if d["displayName"] == db)
    uri = s.get(f"{API}/workspaces/{wsid}/kqlDatabases/{dbid}").json()["properties"]["queryServiceUri"]
    client = KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(uri, cred))

    queries = {
        "count_last_30m": "anomalies | where detected_at > ago(30m) | count",
        "recent": (
            "anomalies | where detected_at > ago(30m) "
            "| summarize n=count(), maxAge=now()-max(detected_at), "
            "minAge=now()-min(detected_at) by machine_id, model_name, sensor_id "
            "| order by machine_id asc"
        ),
        "latest_rows": (
            "anomalies | top 10 by detected_at desc "
            "| project detected_at, machine_id, sensor_id, model_name, score, is_anomaly"
        ),
        "is_anomaly_dist": (
            "anomalies | where detected_at > ago(30m) "
            "| summarize n=count() by is_anomaly"
        ),
    }
    for name, q in queries.items():
        print(f"\n=== {name} ===")
        try:
            r = client.execute(db, q)
            t = r.primary_results[0]
            cols = [c.column_name for c in t.columns]
            print(" | ".join(cols))
            for row in t:
                print(" | ".join(str(row[c]) for c in cols))
        except Exception as exc:  # noqa: BLE001
            print(f"[err] {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
