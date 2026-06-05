"""Count TRUE false positives per machine: landed anomalies that do not match
any ground-truth injection (these are exactly the red dashed lines on the
dashboard). Also counts matched detections (true positives) and missed
injections, per current vs candidate threshold — read-only.

Usage:
    .venv/Scripts/python.exe tools/_count_false_positives.py --lookback 12h
"""

from __future__ import annotations

import argparse
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

LEAD_S = 30
LAG_S = 120


def run(client: KustoClient, db: str, q: str) -> list[dict]:
    t = client.execute(db, q).primary_results[0]
    cols = [c.column_name for c in t.columns]
    return [{c: row[c] for c in cols} for row in t]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", default="12h")
    args = ap.parse_args()
    lb = args.lookback

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

    # A detection is a TRUE POSITIVE if its detected_at falls within any
    # injection interval [start-LEAD, expected_end+LAG] for the same machine.
    q = f"""
    let lookback = {lb};
    let lead = {LEAD_S}s; let lag = {LAG_S}s;
    let dets = anomalies | where detected_at > ago(lookback)
        | project machine_id, detected_at, score;
    let inj = injected_anomalies | where start_ts > ago(lookback)
        | extend lo = start_ts - lead, hi = expected_end_ts + lag
        | project machine_id, lo, hi;
    dets
    | join kind=leftouter (inj) on machine_id
    | extend matched = detected_at >= lo and detected_at <= hi
    | summarize tp = countif(matched), total_join = count() by machine_id, detected_at, score
    | summarize is_tp = max(tp) by machine_id, detected_at, score
    | summarize
        detections = count(),
        true_pos   = countif(is_tp > 0),
        false_pos  = countif(is_tp == 0),
        fp_score_p50 = percentile(iff(is_tp == 0, score, real(null)), 50),
        fp_score_max = max(iff(is_tp == 0, score, real(null)))
      by machine_id
    | order by machine_id asc
    """
    rows = run(client, db, q)
    print(f"=== detections last {lb}: TP (matched injection) vs FP (dashed lines) ===")
    print(f"{'machine':8} {'detections':>10} {'true_pos':>9} {'false_pos':>10} "
          f"{'fp_p50':>9} {'fp_max':>9}")
    for r in rows:
        fp50 = r['fp_score_p50']
        fpmx = r['fp_score_max']
        print(f"{r['machine_id']:8} {r['detections']:10d} {r['true_pos']:9d} "
              f"{r['false_pos']:10d} "
              f"{(fp50 if fp50 is not None else float('nan')):9.3f} "
              f"{(fpmx if fpmx is not None else float('nan')):9.3f}")

    # Injections that produced NO matching detection (missed).
    qm = f"""
    let lookback = {lb};
    let lead = {LEAD_S}s; let lag = {LAG_S}s;
    let dets = anomalies | where detected_at > ago(lookback)
        | project machine_id, detected_at;
    injected_anomalies | where start_ts > ago(lookback)
    | extend lo = start_ts - lead, hi = expected_end_ts + lag
    | join kind=leftouter (dets) on machine_id
    | extend hit = detected_at >= lo and detected_at <= hi
    | summarize hits = countif(hit) by machine_id, start_ts, anomaly_kind
    | summarize injections = count(), detected = countif(hits > 0),
                missed = countif(hits == 0) by machine_id
    | order by machine_id asc
    """
    print(f"\n=== injections last {lb}: detected vs missed ===")
    print(f"{'machine':8} {'injections':>10} {'detected':>9} {'missed':>7}")
    for r in run(client, db, qm):
        print(f"{r['machine_id']:8} {r['injections']:10d} {r['detected']:9d} {r['missed']:7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
