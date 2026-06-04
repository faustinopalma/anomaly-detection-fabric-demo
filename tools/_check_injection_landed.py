"""Quick look at the most recent raw_telemetry spikes per CNC sensor and the
ad-hoc anomaly score over a short lookback, to confirm the manual injections
produced large deviations and crossed the live thresholds.
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

    print("=== raw_telemetry max/min per sensor, last 8 min (M-002/M-003) ===")
    q = (
        "raw_telemetry | where machine_id in ('M-002','M-003') and ts > ago(8m) "
        "| summarize n=count(), mn=min(value), mx=max(value) "
        "by machine_id, sensor_id | order by machine_id asc, sensor_id asc"
    )
    t = client.execute(db, q).primary_results[0]
    for r in t:
        print(f"  {r['machine_id']} {r['sensor_id']:16} n={r['n']:5} "
              f"min={r['mn']:.2f} max={r['mx']:.2f}")

    print("\n=== ad-hoc score over last 8 min (short lookback) ===")
    for model, machine, thr in [
        ("transformer_ae_small__M-002", "M-002", 4.0),
        ("transformer_ae_small__M-003", "M-003", 1.8817),
    ]:
        q = (
            f"score_multivariate_onnx_lookback('{model}','{machine}', 1s, 8m, {thr}) "
            "| summarize n=count(), mx=max(score), nhit=countif(is_anomaly)"
        )
        try:
            r = client.execute(db, q).primary_results[0][0]
            print(f"  {machine} thr={thr}: n={r['n']} max={r['mx']:.3f} hits={r['nhit']}")
        except Exception as e:  # noqa: BLE001
            print(f"  {machine}: query error {e}")

    print("\n=== anomalies landed last 15 min ===")
    q = (
        "anomalies | where detected_at > ago(15m) and machine_id in ('M-002','M-003','M-004') "
        "| summarize n=count(), mx=max(score), recent=max(detected_at) by machine_id, model_name "
        "| order by machine_id asc"
    )
    t = client.execute(db, q).primary_results[0]
    rows = list(t)
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r['machine_id']} {r['model_name']:30} n={r['n']} max={r['mx']:.2f} recent={r['recent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
