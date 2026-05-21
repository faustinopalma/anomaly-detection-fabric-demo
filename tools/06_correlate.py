"""Show ground-truth vs detected anomalies side-by-side.

Calls the `fn_correlate_injections` and `fn_correlation_kpis` KQL functions
deployed by `kql/06_correlation.kql` and prints two tables:
  1) per-injection result (hit/miss, latency)
  2) aggregate KPIs (recall, median/P90 latency)

Usage:
    python tools/06_correlate.py
    python tools/06_correlate.py --lookback 30m --grace 90s
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"


def _connect():
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")
    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]
    cred = get_credential(tenant, SCOPE, repo_root)
    tok = cred.get_token(SCOPE).token
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}"})
    ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
    db = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"] if d["displayName"] == db_name)
    uri = s.get(f"{API}/workspaces/{ws}/kqlDatabases/{db['id']}").json()["properties"]["queryServiceUri"]
    client = KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(uri, cred))
    return client, db_name


def _print_table(rows, columns) -> None:
    if not rows:
        print("  (no rows)")
        return
    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(columns)]
    line = "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns))
    print(line)
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(r[i]).ljust(widths[i]) for i in range(len(columns))))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", default="2h", help="KQL timespan (e.g. 30m, 2h)")
    ap.add_argument("--grace", default="2m", help="Match grace after expected_end_ts (e.g. 90s, 2m)")
    args = ap.parse_args(argv)

    client, db = _connect()

    print(f"\n[Metrics]  lookback={args.lookback}, match_grace={args.grace}")
    q_metrics = f"fn_correlation_metrics({args.grace}, {args.lookback})"
    r = client.execute(db, q_metrics)
    tbl = r.primary_results[0]
    cols = [c.column_name for c in tbl.columns]
    rows = [[("-" if row[c] is None else row[c]) for c in cols] for row in tbl]
    _print_table(rows, cols)

    print(f"\n[KPIs]  lookback={args.lookback}, match_grace={args.grace}")
    q_kpi = f"fn_correlation_kpis({args.grace}, {args.lookback})"
    r = client.execute(db, q_kpi)
    tbl = r.primary_results[0]
    cols = [c.column_name for c in tbl.columns]
    rows = [[row[c] for c in cols] for row in tbl]
    _print_table(rows, cols)

    print(f"\n[Per-injection detail]  TP/FN  lookback={args.lookback}")
    q_inj = f"fn_classify_injections({args.grace}, {args.lookback})"
    r = client.execute(db, q_inj)
    tbl = r.primary_results[0]
    cols = ["start_ts", "machine_id", "anomaly_kind", "sensor_target",
            "duration_s", "label", "latency_s", "det_count", "best_score"]
    rows = []
    for row in tbl:
        vals = []
        for c in cols:
            v = row[c]
            if c == "start_ts" and v is not None:
                v = str(v).replace("T", " ").split(".")[0]
            elif c == "duration_s" and v is not None:
                v = f"{float(v):.1f}"
            elif c == "best_score" and v is not None:
                v = f"{float(v):.4f}"
            elif c == "latency_s" and v is not None:
                v = f"{float(v):.2f}"
            vals.append(v if v is not None else "-")
        rows.append(vals)
    _print_table(rows, cols)

    print(f"\n[Per-detection detail]  TP/FP  lookback={args.lookback}  (top 30)")
    q_det = f"fn_classify_detections({args.grace}, {args.lookback}) | top 30 by detected_at desc"
    r = client.execute(db, q_det)
    tbl = r.primary_results[0]
    cols = ["detected_at", "machine_id", "score", "label",
            "anomaly_kind", "sensor_target"]
    rows = []
    for row in tbl:
        vals = []
        for c in cols:
            v = row[c]
            if c == "detected_at" and v is not None:
                v = str(v).replace("T", " ").split(".")[0]
            elif c == "score" and v is not None:
                v = f"{float(v):.4f}"
            vals.append(v if v is not None else "-")
        rows.append(vals)
    _print_table(rows, cols)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
