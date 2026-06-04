"""Offline sensitivity test: does the M-003 (and M-002) CNC autoencoder react
to a stronger single-sensor injection?

Pulls recent real windows from raw_telemetry, scores them with the local FP16
ONNX model (same path as production), then re-scores after applying synthetic
spikes of increasing magnitude on one sensor. If the score climbs well past the
normal range, strengthening the simulator's injection overlay will make the
machine detectable; if it stays flat, the model itself is insensitive.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
WINDOW = 64
TARGETS = {
    "M-002": "transformer_ae_small__M-002",
    "M-003": "transformer_ae_small__M-003",
}
SPIKE_FACTORS = [0.5, 1.0, 2.0, 3.0, 5.0]


def score(sess: ort.InferenceSession, X: np.ndarray) -> np.ndarray:
    name = sess.get_inputs()[0].name
    return sess.run(None, {name: X.astype(np.float32)})[0].reshape(-1)


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

    for machine, model in TARGETS.items():
        meta = json.loads((root / "models" / model / "metadata.json").read_text())
        sensors = meta["scaler"]["sensors"]
        mean = np.array(meta["scaler"]["mean"], dtype=np.float32)
        std = np.array(meta["scaler"]["std"], dtype=np.float32)
        std = np.where(std == 0, 1.0, std)
        thr = meta["threshold"]
        sess = ort.InferenceSession(str(root / "models" / model / "model.fp16.onnx"))

        # Pull recent rows, pivot to a (T, n_sensors) matrix at 1s bins.
        q = (
            f"raw_telemetry | where machine_id == '{machine}' and ts > ago(20m) "
            f"and sensor_id in ({','.join(repr(s) for s in sensors)}) "
            "| summarize value=avg(value) by bin(ts, 1s), sensor_id "
            "| evaluate pivot(sensor_id, any(value)) | order by ts asc"
        )
        t = client.execute(db, q).primary_results[0]
        cols = [c.column_name for c in t.columns]
        rows = [{c: row[c] for c in cols} for row in t]
        mat = np.array([[float(r[se]) if r.get(se) is not None else 0.0 for se in sensors]
                        for r in rows], dtype=np.float32)
        if mat.shape[0] < WINDOW:
            print(f"{machine}: only {mat.shape[0]} bins, need {WINDOW}\n")
            continue
        # Build a handful of non-overlapping clean windows.
        n_win = min(12, mat.shape[0] // WINDOW)
        wins = np.stack([mat[i * WINDOW:(i + 1) * WINDOW] for i in range(n_win)])  # (n,64,3)
        Xn = (wins - mean) / std
        base = score(sess, Xn)
        print(f"--- {machine} ({model}) thr={thr:.3f} ---")
        print(f"  clean windows n={n_win} base score: "
              f"min={base.min():.3f} mean={base.mean():.3f} max={base.max():.3f}")
        # Use the most ACTIVE window (largest mean |value|) so an idle/near-zero
        # window doesn't mask the model's response.
        activity = np.abs(wins).reshape(n_win, -1).mean(axis=1)
        w0 = wins[int(np.argmax(activity))].copy()
        # ADDITIVE spike in units of the per-sensor std (scale-invariant; works
        # even on idle sensors). k = +k*std on the second half of the window.
        for si, sname in enumerate(sensors):
            line = []
            for k in SPIKE_FACTORS:
                w = w0.copy()
                w[WINDOW // 2:, si] = w[WINDOW // 2:, si] + k * float(std[si])
                Xs = ((w - mean) / std)[None, :, :]
                sc = score(sess, Xs)[0]
                line.append(f"+{k}std={sc:.2f}")
            print(f"  add[{sname:16}]: " + "  ".join(line))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
