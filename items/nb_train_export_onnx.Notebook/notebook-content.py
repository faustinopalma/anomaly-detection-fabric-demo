# Fabric notebook source
#
# nb-train-export-onnx
# --------------------
# Trains an anomaly-detection model on the gold windows and exports it to
# ONNX. The serialized bytes are uploaded to the KQL DB `models` table so
# the in-Kusto scoring functions can use it.
#
# Choose ONE of the two example trainers below (univariate / multivariate)
# and adjust the architecture as needed. Both end with the same export +
# upload pattern.

# CELL ********************

# META { "language": "python", "language_group": "synapse_pyspark" }

MODEL_NAME    = "univariate_ae"   # or "multivariate_iforest"
WINDOW_SIZE   = 64
SENSORS       = []                # fill for multivariate, e.g. ["temp", "vib", "pressure"]
KQL_CLUSTER   = "<eventhouse-query-uri>"   # e.g. https://<eh-id>.kusto.fabric.microsoft.com
KQL_DATABASE  = "kql-telemetry"

# CELL ********************

import numpy as np
import pandas as pd

# Load training data from the lakehouse gold layer.
src_table = "lh_telemetry.gold_windows_uni" if not SENSORS else "lh_telemetry.gold_windows_multi"
pdf = spark.read.table(src_table).toPandas()

if SENSORS:
    # rows: list[dict[sensor -> value]]; convert to ndarray [N, window, n_sensors]
    X = np.stack([
        np.array([[step.get(s, 0.0) for s in SENSORS] for step in row], dtype=np.float32)
        for row in pdf["rows"]
    ])
else:
    X = np.stack([np.array(v, dtype=np.float32) for v in pdf["values"]])
    X = X.reshape(X.shape[0], X.shape[1], 1)

print("Training shape:", X.shape)

# CELL ********************

# Example trainer: tiny LSTM autoencoder in PyTorch (works for both shapes).
import torch
from torch import nn

class WindowAE(nn.Module):
    def __init__(self, n_features, hidden=32):
        super().__init__()
        self.enc = nn.LSTM(n_features, hidden, batch_first=True)
        self.dec = nn.LSTM(hidden, n_features, batch_first=True)

    def forward(self, x):
        h, _ = self.enc(x)
        out, _ = self.dec(h)
        return out

model = WindowAE(n_features=X.shape[2])
opt   = torch.optim.Adam(model.parameters(), lr=1e-3)
loss  = nn.MSELoss()

t = torch.from_numpy(X)
for epoch in range(20):
    opt.zero_grad()
    recon = model(t)
    e = loss(recon, t)
    e.backward()
    opt.step()
    if epoch % 5 == 0:
        print(f"epoch {epoch} loss {e.item():.5f}")

# CELL ********************

# Export to ONNX. We export a "score" model that returns the per-sample
# reconstruction error so KQL gets a single anomaly score per row.
import io, base64

class ScoreWrapper(nn.Module):
    def __init__(self, ae):
        super().__init__()
        self.ae = ae

    def forward(self, x):
        recon = self.ae(x)
        # mean squared error over (window, features) -> [batch]
        return ((recon - x) ** 2).mean(dim=(1, 2))

wrapped = ScoreWrapper(model).eval()
dummy   = torch.zeros((1, WINDOW_SIZE, X.shape[2]), dtype=torch.float32)

buf = io.BytesIO()
torch.onnx.export(
    wrapped, dummy, buf,
    input_names=["window"], output_names=["score"],
    dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
    opset_version=17,
)
onnx_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
print("ONNX size:", len(buf.getvalue()) / 1024, "KB")

# CELL ********************

# Upload to the KQL `models` table (versioned).
# Uses azure-kusto-data with the notebook's identity (AAD).
from azure.kusto.data        import KustoConnectionStringBuilder, KustoClient
from azure.kusto.data.helpers import dataframe_from_result_table
from azure.kusto.ingest      import QueuedIngestClient, IngestionProperties, DataFormat
import json, datetime as dt

kcsb   = KustoConnectionStringBuilder.with_aad_device_authentication(KQL_CLUSTER)
client = KustoClient(kcsb)

# Determine next version
res = client.execute(KQL_DATABASE,
    f"models | where name == '{MODEL_NAME}' | summarize v = max_of(0, max(version))")
next_version = int(dataframe_from_result_table(res.primary_results[0])["v"].iloc[0]) + 1

row = pd.DataFrame([{
    "name":        MODEL_NAME,
    "version":     next_version,
    "created_at":  dt.datetime.utcnow(),
    "framework":   "onnx",
    "window_size": WINDOW_SIZE,
    "sensors":     json.dumps(SENSORS) if SENSORS else None,
    "payload":     onnx_b64,
    "metadata":    json.dumps({"final_loss": float(e.item())}),
}])

ingest = QueuedIngestClient(KustoConnectionStringBuilder.with_aad_device_authentication(
    KQL_CLUSTER.replace("https://", "https://ingest-")))
ingest.ingest_from_dataframe(row, IngestionProperties(
    database=KQL_DATABASE, table="models", data_format=DataFormat.CSV))

print(f"Uploaded {MODEL_NAME} v{next_version}")
