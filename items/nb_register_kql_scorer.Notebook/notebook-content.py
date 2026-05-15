# Fabric notebook source
#
# nb-register-kql-scorer
# ----------------------
# Re-applies the KQL schema scripts (kql/*.kql) against the KQL database.
# Use this after retraining when the scoring function signature or update
# policy changes. Idempotent: scripts use .create-or-alter / .alter-merge.

# CELL ********************

# META { "language": "python", "language_group": "synapse_pyspark" }

KQL_CLUSTER  = "<eventhouse-query-uri>"
KQL_DATABASE = "kql-telemetry"

# CELL ********************

import os

from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

kcsb   = KustoConnectionStringBuilder.with_aad_device_authentication(KQL_CLUSTER)
client = KustoClient(kcsb)

# The notebook resource files live alongside this notebook in the repo.
# When run from Fabric, paste the contents inline or read from the lakehouse
# Files area where the kql/ folder has been uploaded.
KQL_DIR = "/lakehouse/default/Files/kql"

for fname in sorted(os.listdir(KQL_DIR)):
    if not fname.endswith(".kql"):
        continue
    path = os.path.join(KQL_DIR, fname)
    with open(path, "r", encoding="utf-8") as f:
        script = f.read()
    print(f"--- Applying {fname} ---")
    # Each file may contain multiple control commands separated by blank lines.
    for stmt in [s.strip() for s in script.split("\n\n") if s.strip() and not s.strip().startswith("//")]:
        client.execute_mgmt(KQL_DATABASE, stmt)
    print("ok")
