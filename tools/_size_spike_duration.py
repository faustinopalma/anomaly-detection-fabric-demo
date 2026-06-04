"""Find the minimum spike *duration* (sample count within a 64-wide window)
that pushes the M-002 / M-003 multivariate score across the live threshold,
using real recent windows. This sizes BASE_DURATION['spike'] so brief CNC
spikes are not diluted away by the window-averaged reconstruction error.
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
SPIKE_SIGMA_K = 1.3
LEVEL_MAG = {1: 0.6, 3: 1.0, 5: 1.8}
SAMPLE_COUNTS = [5, 10, 16, 24, 32, 48]
TARGETS = [
    ("transformer_ae_small__M-002", "M-002", 4.0),
    ("transformer_ae_small__M-003", "M-003", 1.8817),
]


def score(sess, X):
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

    for model, machine, thr in TARGETS:
        meta = json.loads((root / "models" / model / "metadata.json").read_text())
        sensors = meta["scaler"]["sensors"]
        mean = np.array(meta["scaler"]["mean"], dtype=np.float32)
        std = np.array(meta["scaler"]["std"], dtype=np.float32)
        std = np.where(std == 0, 1.0, std)
        sess = ort.InferenceSession(str(root / "models" / model / "model.fp16.onnx"))

        q = (
            f"raw_telemetry | where machine_id == '{machine}' and ts > ago(20m) "
            f"and sensor_id in ({','.join(repr(x) for x in sensors)}) "
            "| summarize value=avg(value) by bin(ts, 1s), sensor_id "
            "| evaluate pivot(sensor_id, any(value)) | order by ts asc"
        )
        t = client.execute(db, q).primary_results[0]
        cols = [c.column_name for c in t.columns]
        rows = [{c: row[c] for c in cols} for row in t]
        mat = np.array([[float(r[se]) if r.get(se) is not None else 0.0 for se in sensors]
                        for r in rows], dtype=np.float32)
        n_win = min(12, mat.shape[0] // WINDOW)
        wins = np.stack([mat[i * WINDOW:(i + 1) * WINDOW] for i in range(n_win)])
        w0 = wins[int(np.abs(wins).reshape(n_win, -1).mean(1).argmax())].copy()
        sigma = w0.std(axis=0)
        # Most reactive sensor = torque (largest index typically); test all.
        print(f"--- {machine} thr={thr} ---  (rows for n spiked samples -> score)")
        header = "  dur(s) " + "  ".join(f"L{l}" for l in LEVEL_MAG)
        for si, sname in enumerate(sensors):
            print(f"  sensor={sname}")
            print(header)
            for k in SAMPLE_COUNTS:
                cells = []
                for lvl, mag in LEVEL_MAG.items():
                    w = w0.copy()
                    seg = slice(WINDOW - k, WINDOW)
                    v = w[seg, si]
                    amp = np.maximum.reduce([
                        np.abs(v) * 0.5, np.ones_like(v),
                        np.full_like(v, SPIKE_SIGMA_K * float(sigma[si])),
                    ]) * (1.5 + 1.5 * mag)
                    sign = np.where(v >= 0, 1.0, -1.0)
                    w[seg, si] = v + sign * amp
                    sc = score(sess, ((w - mean) / std)[None])[0]
                    hit = "OK" if sc > thr else "  "
                    cells.append(f"{sc:6.1f}{hit}")
                print(f"  {k:5}  " + " ".join(cells))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
