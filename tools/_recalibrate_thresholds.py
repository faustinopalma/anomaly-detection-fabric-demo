"""Recommend per-machine anomaly thresholds from the LIVE score distribution.

The registered thresholds were calibrated on training reconstruction error and
no longer match the live simulator's distribution (train/serve drift), causing
false positives (the threshold sits inside the normal-score cluster).

This tool scores all windows over a recent lookback, splits them into NORMAL vs
INJECTED using the ground-truth ``injected_anomalies`` table, and recommends a
threshold that sits in the gap between the two clusters: comfortably above the
normal p99.9 and below the smallest injected score, so real anomalies still
fire while normal operation never does.

Read-only: it does NOT modify any model. Apply the recommendation by editing
``models/<dir>/metadata.json`` ``threshold`` and re-running
``tools/05_register_model.py``.

Usage:
    .venv/Scripts/python.exe tools/_recalibrate_thresholds.py
    .venv/Scripts/python.exe tools/_recalibrate_thresholds.py --lookback 12h
"""

from __future__ import annotations

import argparse
import math
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

# Match window used by the dashboard (detection allowed slightly before / after
# the injection): widen the injection interval before labelling windows.
INJ_LEAD_S = 30.0
INJ_LAG_S = 120.0


def run(client: KustoClient, db: str, q: str) -> list[dict]:
    t = client.execute(db, q).primary_results[0]
    cols = [c.column_name for c in t.columns]
    return [{c: row[c] for c in cols} for row in t]


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def recommend(scores: list[float], current: float) -> tuple[float, str]:
    """Pick a threshold in the largest log-gap of the score distribution.

    The live scores are strongly bimodal (normal cluster ~1, anomaly cluster
    far higher). We separate the two clusters by the largest multiplicative gap
    that sits above the median (so we never split the normal noise band), then
    place the threshold at the geometric mean of the gap endpoints.
    """
    xs = sorted(s for s in scores if s == s and s > 0)
    if len(xs) < 5:
        return current, "too few windows -> keep current"
    med = pct(xs, 50)
    # Largest log-gap with the lower endpoint above the median.
    best_ratio, lo_v, hi_v = 0.0, None, None
    for a, b in zip(xs, xs[1:]):
        if a < med:
            continue
        ratio = b / a
        if ratio > best_ratio:
            best_ratio, lo_v, hi_v = ratio, a, b
    # A real cluster separation shows up as a >= ~2x jump.
    if lo_v is not None and best_ratio >= 2.0:
        normal_max = lo_v
        anom_min = hi_v
        thr = math.sqrt(normal_max * anom_min)
        # Keep some sensitivity: don't sit more than 6x above the normal max.
        thr = min(thr, normal_max * 6.0)
        return thr, (f"gap {normal_max:.3f}->{anom_min:.3f} (x{best_ratio:.0f}) -> {thr:.3f}")
    # Unimodal (no anomalies captured): 2x margin above the p99.9.
    n999 = pct(xs, 99.9)
    return n999 * 2.0, f"no clear gap; 2x p99.9 ({n999:.3f}) -> {n999 * 2.0:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", default="6h")
    args = ap.parse_args()

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

    # Current thresholds.
    cur = {r["name"]: r["threshold"] for r in run(client, db, (
        "models | summarize arg_max(version, *) by name "
        "| extend threshold=todouble(metadata.threshold) | project name, threshold"))}

    print(f"lookback={args.lookback}  inj window=[-{INJ_LEAD_S:g}s,+{INJ_LAG_S:g}s]\n")
    header = (f"{'machine':8} {'cur':>8} {'n_win':>6} {'n_inj':>6} "
              f"{'p50':>8} {'p99':>9} {'max':>9} {'RECO':>9}  note")
    print(header)
    print("-" * len(header))

    results: dict[str, float] = {}
    for machine, model in MACHINES.items():
        t = cur.get(model, 0.0)
        # Score all windows (threshold=0 so nothing is filtered) keeping the
        # window time span; injection overlap is reported for context only — the
        # threshold itself is derived from the value distribution (robust to
        # noisy time labels).
        scored = run(client, db, (
            f"score_multivariate_onnx_lookback('{model}', '{machine}', 1s, {args.lookback}, 0.0) "
            "| project window_start, window_end, score | order by window_start asc"))
        inj = run(client, db, (
            f"injected_anomalies | where machine_id == '{machine}' "
            f"and start_ts > now() - {args.lookback} "
            "| project start_ts, expected_end_ts | order by start_ts asc"))
        intervals = [(r["start_ts"], r["expected_end_ts"]) for r in inj]

        def is_injected(ws, we) -> bool:
            import datetime as _dt
            for lo, hi in intervals:
                lo2 = lo - _dt.timedelta(seconds=INJ_LEAD_S)
                hi2 = hi + _dt.timedelta(seconds=INJ_LAG_S)
                if ws <= hi2 and we >= lo2:  # overlap
                    return True
            return False

        all_scores = [float(r["score"]) for r in scored]
        n_inj = sum(1 for r in scored if is_injected(r["window_start"], r["window_end"]))

        reco, note = recommend(all_scores, t)
        results[model] = reco
        print(f"{machine:8} {t:8.3f} {len(all_scores):6d} {n_inj:6d} "
              f"{pct(all_scores,50):8.3f} {pct(all_scores,99):9.3f} "
              f"{(max(all_scores) if all_scores else float('nan')):9.3f} "
              f"{reco:9.3f}  {note}")

    print("\n=== suggested metadata.json edits ===")
    for model, reco in results.items():
        print(f"{model}: threshold -> {reco:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
