"""Fetch AML run error details + child runs via the run-history REST API
(metadata only, no storage/firewall needed)."""
from __future__ import annotations
import json
import sys
import requests
from azure.identity import AzureCliCredential

SUB = "7ecf802f-04ac-4e81-8703-c3d39074f823"
RG = "rg-anomaly-ml-westeurope"
WS = "anomalyml-mlw"
REGION = "westeurope"
JOB = sys.argv[1] if len(sys.argv) > 1 else "boring_shirt_cts9nd241f"

cred = AzureCliCredential()
tok = cred.get_token("https://management.azure.com/.default").token
h = {"Authorization": f"Bearer {tok}"}
base = (f"https://{REGION}.api.azureml.ms/history/v1.0/subscriptions/{SUB}"
        f"/resourceGroups/{RG}/providers/Microsoft.MachineLearningServices"
        f"/workspaces/{WS}")

# Top-level run details
r = requests.get(f"{base}/experiments/_/runs/{JOB}/details", headers=h)
d = r.json()
print("=== run", JOB, "status:", d.get("status"), "===")
err = d.get("error") or {}
print("error:", json.dumps(err, indent=2)[:2000])
print("warnings:", json.dumps(d.get("warnings"), indent=2)[:500])

# Child runs (image build etc.)
rc = requests.post(f"{base}/runs", headers=h,
                   json={"filter": f"rootRunId eq '{JOB}'"})
try:
    children = rc.json().get("value", [])
    print(f"\n=== {len(children)} child run(s) ===")
    for ch in children:
        print(ch.get("runId"), ch.get("status"),
              (ch.get("error") or {}).get("error", {}).get("message", "")[:300])
except Exception as e:
    print("child fetch error:", e, rc.text[:300])
