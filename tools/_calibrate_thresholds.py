"""Score histogram per machine to decide threshold calibration.

Buckets the lookback scores into log-ish bands so we can see whether an
injection cluster is separable from the normal cluster, and computes the
NORMAL-only distribution (scores below an injection cutoff) used to set a
principled threshold = max(normal_p999, normal_max * margin).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
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
LOOKBACK = "3h"
# Below this score a window is treated as "normal" for calibration. Chosen to
# sit in the obvious gap between the normal cluster (<~3) and injection cluster
# (hundreds). For CNC machines with no big spikes this keeps the whole range.
INJECTION_CUTOFF = 10.0
MARGIN = 1.5  # threshold = max(normal p99.9, normal max * MARGIN)
BANDS = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 100.0, float("inf")]
BAND_LABELS = ["<0.5", "0.5-1", "1-1.5", "1.5-2", "2-3", "3-5", "5-10", "10-100", ">=100"]


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

    print(f"lookback={LOOKBACK} injection_cutoff={INJECTION_CUTOFF} margin={MARGIN}\n")
    suggestions: dict[str, float] = {}
    for machine, model in MACHINES.items():
        q = (
            f"score_multivariate_onnx_lookback('{model}', '{machine}', 1s, {LOOKBACK}, 0.0) "
            "| project score"
        )
        t = client.execute(db, q).primary_results[0]
        scores = np.array([row["score"] for row in t], dtype=float)
        if scores.size == 0:
            print(f"--- {machine} ({model}) --- no windows\n")
            continue
        normal = scores[scores < INJECTION_CUTOFF]
        inj = scores[scores >= INJECTION_CUTOFF]
        counts = np.histogram(scores, bins=np.array([0.0] + BANDS))[0]
        nmax = float(normal.max()) if normal.size else 0.0
        np999 = float(np.percentile(normal, 99.9)) if normal.size else 0.0
        suggested = max(np999, nmax * MARGIN)
        suggestions[model] = suggested
        print(f"--- {machine} ({model}) ---")
        print("  bands: " + ", ".join(f"{l}={c}" for l, c in zip(BAND_LABELS, counts) if c))
        print(f"  n_all={scores.size} n_inj(>= {INJECTION_CUTOFF})={inj.size}"
              + (f" inj_min={inj.min():.3f}" if inj.size else ""))
        print(f"  normal: p50={np.percentile(normal,50):.3f} "
              f"p90={np.percentile(normal,90):.3f} "
              f"p99={np.percentile(normal,99):.3f} max={nmax:.3f} p99.9={np999:.3f}")
        print(f"  >>> SUGGESTED THRESHOLD = {suggested:.3f}\n")

    print("=== summary (model -> suggested threshold) ===")
    for model, thr in suggestions.items():
        print(f"  {model}: {thr:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
