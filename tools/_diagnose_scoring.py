"""End-to-end scoring diagnostic.

For each machine, reports:
  * the live model registered (version, threshold from metadata, #sensors),
  * raw_telemetry freshness/volume,
  * the score distribution over a recent lookback (via the ad-hoc
    score_multivariate_onnx_lookback function) vs the live threshold,
  * how many recent anomalies actually landed in the `anomalies` table.

This pinpoints mis-calibrated thresholds (too low -> false positives,
too high -> silent) without touching the live environment.
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

MACHINES = {
    "M-001": "transformer_ae_small__M-001",
    "M-002": "transformer_ae_small__M-002",
    "M-003": "transformer_ae_small__M-003",
    "M-004": "transformer_ae_small__M-004",
}

LOOKBACK = "2h"


def run(client: KustoClient, db: str, q: str) -> list[dict]:
    t = client.execute(db, q).primary_results[0]
    cols = [c.column_name for c in t.columns]
    return [{c: row[c] for c in cols} for row in t]


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

    print("=== registered models (latest version) ===")
    rows = run(client, db, (
        "models | summarize arg_max(version, *) by name "
        "| extend threshold=todouble(metadata.threshold), n_sensors=array_length(sensors) "
        "| project name, version, threshold, n_sensors, sensors=tostring(sensors)"
    ))
    thr: dict[str, float] = {}
    for r in rows:
        thr[r["name"]] = r["threshold"]
        print(f"{r['name']:32} v{r['version']} thr={r['threshold']:.4f} "
              f"n_sensors={r['n_sensors']} {r['sensors']}")

    print("\n=== raw_telemetry freshness (last 10m) ===")
    rows = run(client, db, (
        "raw_telemetry | where ts > ago(10m) "
        "| summarize n=count(), nsens=dcount(sensor_id), maxAge=now()-max(ts) "
        "by machine_id | order by machine_id asc"
    ))
    for r in rows:
        print(f"{r['machine_id']}  rows={r['n']:>6}  sensors={r['nsens']}  maxAge={r['maxAge']}")

    print(f"\n=== score distribution per machine over last {LOOKBACK} (ad-hoc lookback) ===")
    print(f"{'machine':8} {'model':32} {'thr':>8} {'n':>5} "
          f"{'min':>8} {'p50':>8} {'p90':>8} {'p99':>8} {'max':>8} {'%>thr':>7}")
    for machine, model in MACHINES.items():
        t = thr.get(model, 0.0)
        q = (
            f"score_multivariate_onnx_lookback('{model}', '{machine}', 1s, {LOOKBACK}, {t}) "
            "| summarize n=count(), mn=min(score), p50=percentile(score,50), "
            "p90=percentile(score,90), p99=percentile(score,99), mx=max(score), "
            "n_anom=countif(score > real(" + f"{t}" + "))"
        )
        try:
            r = run(client, db, q)[0]
            n = r["n"] or 0
            pct = (100.0 * (r["n_anom"] or 0) / n) if n else 0.0
            if n:
                print(f"{machine:8} {model:32} {t:8.3f} {n:5} "
                      f"{r['mn']:8.3f} {r['p50']:8.3f} {r['p90']:8.3f} "
                      f"{r['p99']:8.3f} {r['mx']:8.3f} {pct:6.1f}%")
            else:
                print(f"{machine:8} {model:32} {t:8.3f} {n:5}  (no windows scored)")
        except Exception as exc:  # noqa: BLE001
            print(f"{machine:8} {model:32} {t:8.3f}  [err] {exc}")

    print("\n=== anomalies landed (last 2h) — these are detections that fired ===")
    rows = run(client, db, (
        "anomalies | where detected_at > ago(2h) "
        "| summarize n=count(), mn=min(score), p50=percentile(score,50), mx=max(score), "
        "maxAge=now()-max(detected_at) by machine_id, model_name "
        "| order by machine_id asc"
    ))
    if not rows:
        print("(none)")
    for r in rows:
        print(f"{r['machine_id']}  {r['model_name']:32}  n={r['n']:>4}  "
              f"min={r['mn']:.3f}  p50={r['p50']:.3f}  max={r['mx']:.3f}  maxAge={r['maxAge']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
