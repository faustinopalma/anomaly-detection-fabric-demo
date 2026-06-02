"""Create (or update) a Fabric Real-Time Dashboard (KQLDashboard) wired to
`kql_telemetry`, with curated tiles for live machine telemetry.

Idempotent: if a dashboard with the same name already exists in the workspace,
its definition is replaced.

Usage:
    python tools/04_create_dashboard.py
    python tools/04_create_dashboard.py --name "Iveco Live Telemetry"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
DEFAULT_NAME = "rtd_telemetry_live"


# ---------------------------------------------------------------------------
# KQL fragments
# ---------------------------------------------------------------------------

Q_KPIS = """\
let scope = raw_telemetry
  | where ts between (_startTime .. _endTime)
  | where isempty(['_machine']) or machine_id == ['_machine'];
scope
| summarize
    ['Events'] = count(),
    ['Machines'] = dcount(machine_id),
    ['Sensors'] = dcount(sensor_id),
    ['Latest event (UTC)'] = format_datetime(max(ts), 'yyyy-MM-dd HH:mm:ss')
"""

Q_TEMP_MOTOR = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'temperature_motor'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Temperature motor [°C]')
"""

Q_POWER = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'power'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Power [kW]')
"""

Q_VIB = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'vibration_axial'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Vibration axial [mm/s]')
"""

Q_LATEST = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize arg_max(ts, value) by machine_id, sensor_id
| project machine_id, sensor_id, value = round(value, 3), ts
| order by machine_id asc, sensor_id asc
"""

Q_ANOMALY_ZSCORE = """\
let baseline = raw_telemetry
  | where ts between (ago(1d) .. _endTime)
  | where isempty(['_machine']) or machine_id == ['_machine']
  | summarize mean=avg(value), sd=stdev(value) by machine_id, sensor_id;
raw_telemetry
| where ts between (_startTime .. _endTime)
| where isempty(['_machine']) or machine_id == ['_machine']
| join kind=inner baseline on machine_id, sensor_id
| extend zscore = iff(sd > 0.0, abs(value - mean) / sd, real(0))
| where zscore > 2.0
| project ts, machine_id, sensor_id, value = round(value, 3), zscore = round(zscore, 2)
| top 50 by zscore desc
"""

Q_MACHINE_LIST = """\
raw_telemetry
| where ts > ago(1d)
| distinct machine_id
| order by machine_id asc
"""


# ---------------------------------------------------------------------------
# CNC (M-003) sensor tiles — the CNC machine has its own 3-sensor set
# (mandrino_load %, mandrino_power kW, mandrino_torque N*cm) that is not
# covered by the synthetic-machine charts above.
# ---------------------------------------------------------------------------

Q_CNC_LOAD = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'mandrino_load'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Spindle load [%]')
"""

Q_CNC_POWER = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'mandrino_power'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Spindle power [kW]')
"""

Q_CNC_TORQUE = """\
raw_telemetry
| where ts between (_startTime .. _endTime)
| where sensor_id == 'mandrino_torque'
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize avg_value = avg(value) by machine_id, bin(ts, 10s)
| order by ts asc
| render timechart with (ytitle='Spindle torque [N*cm]')
"""



# ---------------------------------------------------------------------------
# Ground-truth correlation tiles
# ---------------------------------------------------------------------------

Q_CORR_KPIS = """\
fn_correlation_kpis(2m, _endTime - _startTime)
"""

Q_CORR_TIMELINE = """\
let scope_start = _startTime;
let scope_end   = _endTime;
let injections = injected_anomalies
    | where start_ts between (scope_start .. scope_end)
    | where isempty(['_machine']) or machine_id == ['_machine']
    | project ts = start_ts, machine_id,
              series = strcat('INJECTED ', anomaly_kind),
              value = 1.0;
let detections = anomalies
    | where detected_at between (scope_start .. scope_end)
    | where isempty(['_machine']) or machine_id == ['_machine']
    | where is_anomaly == true
    | project ts = detected_at, machine_id,
              series = strcat('DETECTED ', model_name),
              value = score;
union injections, detections
| order by ts asc
| render timechart with (ytitle='Injections (1.0) vs detection score', title='Ground truth vs model detections')
"""

Q_CORR_TABLE = """\
fn_correlate_injections(2m, _endTime - _startTime)
| where isempty(['_machine']) or machine_id == ['_machine']
| project
    start_ts,
    machine_id,
    anomaly_kind,
    sensor_target,
    duration_s = round(duration_s, 1),
    hit,
    latency_s  = round(latency_s, 2),
    best_score = round(best_score, 4),
    first_det_at
| top 50 by start_ts desc
"""

Q_METRICS = """\
fn_correlation_metrics(2m, _endTime - _startTime)
"""

Q_METRICS_BY_MACHINE = """\
fn_correlation_metrics_by_machine(2m, _endTime - _startTime)
| where isempty(['_machine']) or ['Machine'] == ['_machine']
"""

Q_TPFP_TIMELINE = """\
fn_classify_detections(2m, _endTime - _startTime)
| where isempty(['_machine']) or machine_id == ['_machine']
| summarize cnt = count() by bin(detected_at, 1m), label
| order by detected_at asc
| render timechart with (ytitle='Detections / min', title='TP vs FP over time')
"""

Q_FN_TABLE = """\
fn_classify_injections(2m, _endTime - _startTime)
| where label == 'FN'
| where isempty(['_machine']) or machine_id == ['_machine']
| project start_ts, machine_id, anomaly_kind, sensor_target,
          duration_s = round(duration_s, 1), expected_end_ts
| top 50 by start_ts desc
"""

Q_FP_TABLE = """\
fn_classify_detections(2m, _endTime - _startTime)
| where label == 'FP'
| where isempty(['_machine']) or machine_id == ['_machine']
| project detected_at, machine_id, score = round(score, 4), model_name
| top 50 by detected_at desc
"""



# ---------------------------------------------------------------------------
# Dashboard builder
# ---------------------------------------------------------------------------

def _id() -> str:
    return str(uuid.uuid4())


def build_dashboard(workspace_id: str, database_id: str, cluster_uri: str, database: str, title: str) -> dict:
    ds_id = _id()
    page_id = _id()
    p_time_id = _id()
    p_machine_id = _id()
    q_machines_id = _id()

    def tile(title_, desc, x, y, w, h, visual, query_text, options=None):
        qid = _id()
        return (
            {
                "id": _id(),
                "title": title_,
                "description": desc,
                "visualType": visual,
                "pageId": page_id,
                "layout": {"x": x, "y": y, "width": w, "height": h},
                "queryRef": {"kind": "query", "queryId": qid},
                "visualOptions": options or {},
            },
            {
                "id": qid,
                "dataSource": {"kind": "inline", "dataSourceId": ds_id},
                "text": query_text,
                "usedVariables": ["_startTime", "_endTime", "_machine"],
            },
        )

    line_opts = {
        "multipleYAxes": {
            "base": {
                "id": "-1", "label": "", "columns": [],
                "yAxisMaximumValue": None, "yAxisMinimumValue": None,
                "yAxisScale": "linear", "horizontalLines": [],
            },
            "additional": [], "showMultiplePanels": False,
        },
        "hideLegend": False,
        "legendLocation": "bottom",
        "xColumnTitle": "Time",
        "xColumn": None,
        "yColumns": None,
        "seriesColumns": None,
        "xAxisScale": "linear",
        "verticalLine": "",
        "crossFilterDisabled": True,
        "drillthroughDisabled": True,
        "crossFilter": [],
        "drillthrough": [],
    }
    table_opts = {
        "table__enableRenderLinks": True,
        "colorRulesDisabled": True,
        "colorRules": [],
        "colorStyle": "light",
        "crossFilterDisabled": True,
        "drillthroughDisabled": True,
        "crossFilter": [],
        "drillthrough": [],
        "table__renderLinks": [],
    }
    multistat_opts = {
        "multiStat__textSize": "auto",
        "multiStat__valueColumn": None,
        "colorRulesDisabled": True,
        "colorStyle": "light",
        "multiStat__displayOrientation": "horizontal",
        "multiStat__labelColumn": None,
        "multiStat__slot": {"width": 4, "height": 1},
        "colorRules": [],
    }

    tiles_and_queries = [
        tile("Overview KPIs", "Counts in the selected time range",
             0, 0, 24, 3, "multistat", Q_KPIS, multistat_opts),
        tile("Temperature motor [°C]", "Avg per machine, 10s bins",
             0, 3, 12, 7, "line", Q_TEMP_MOTOR, line_opts),
        tile("Power [kW]", "Avg per machine, 10s bins",
             12, 3, 12, 7, "line", Q_POWER, line_opts),
        tile("Vibration axial [mm/s]", "Avg per machine, 10s bins",
             0, 10, 12, 7, "line", Q_VIB, line_opts),
        tile("Top z-score anomalies", "Baseline: last 24h. Threshold z > 2",
             12, 10, 12, 7, "table", Q_ANOMALY_ZSCORE, table_opts),
        tile("Latest readings", "Last value per machine x sensor in range",
             0, 17, 24, 7, "table", Q_LATEST, table_opts),
        tile("Correlation KPIs", "Injected vs detected (uses fn_correlation_kpis)",
             0, 24, 24, 3, "multistat", Q_CORR_KPIS, multistat_opts),
        tile("Ground truth vs detections (timeline)",
             "Injection markers (value=1) overlaid on detection scores",
             0, 27, 24, 7, "line", Q_CORR_TIMELINE, line_opts),
        tile("Injection results (hit / miss / latency)",
             "Per-injection correlation; latency_s = time-to-detect",
             0, 34, 24, 8, "table", Q_CORR_TABLE, table_opts),
        tile("Precision / Recall / F1",
             "TP/FP/FN with precision, recall, F1 and detection latency",
             0, 42, 24, 3, "multistat", Q_METRICS, multistat_opts),
        tile("Model quality by machine",
             "Per-machine precision / recall / F1 / latency for ALL machines (M-001..M-004)",
             0, 45, 24, 6, "table", Q_METRICS_BY_MACHINE, table_opts),
        tile("TP vs FP over time",
             "Per-minute count of true-positive vs false-positive detections",
             0, 51, 12, 7, "line", Q_TPFP_TIMELINE, line_opts),
        tile("False negatives (missed injections)",
             "Injections with no detection inside [start_ts .. expected_end_ts + 2m]",
             12, 51, 12, 7, "table", Q_FN_TABLE, table_opts),
        tile("False positives (spurious detections)",
             "Detections outside any injection window",
             0, 58, 24, 7, "table", Q_FP_TABLE, table_opts),
        # --- CNC (M-003) sensor charts: only machines with these sensors show ---
        tile("Spindle load [%] (CNC)", "mandrino_load — avg per machine, 10s bins",
             0, 65, 8, 7, "line", Q_CNC_LOAD, line_opts),
        tile("Spindle power [kW] (CNC)", "mandrino_power — avg per machine, 10s bins",
             8, 65, 8, 7, "line", Q_CNC_POWER, line_opts),
        tile("Spindle torque [N*cm] (CNC)", "mandrino_torque — avg per machine, 10s bins",
             16, 65, 8, 7, "line", Q_CNC_TORQUE, line_opts),
    ]
    tiles = [t for t, _q in tiles_and_queries]
    queries = [q for _t, q in tiles_and_queries]

    # Helper query to populate the machine_id dropdown
    queries.append({
        "id": q_machines_id,
        "dataSource": {"kind": "inline", "dataSourceId": ds_id},
        "text": Q_MACHINE_LIST,
        "usedVariables": [],
    })

    return {
        "schema_version": 63,
        "title": title,
        "autoRefresh": {"enabled": True, "defaultInterval": "30s"},
        "baseQueries": [],
        "tiles": tiles,
        "dataSources": [{
            "kind": "manual-kusto",
            "scopeId": "kusto",
            "id": ds_id,
            "name": database,
            "clusterUri": cluster_uri,
            "database": database,
            "databaseArtifactId": database_id,
            "workspaceArtifactId": workspace_id,
            "queryResultsCacheMaxAge": 0,
        }],
        "pages": [{"name": "Telemetry Live", "id": page_id}],
        "parameters": [
            {
                "kind": "duration",
                "id": p_time_id,
                "displayName": "Time range",
                "description": "",
                "beginVariableName": "_startTime",
                "endVariableName": "_endTime",
                "defaultValue": {"kind": "dynamic", "count": 30, "unit": "minutes"},
                "showOnPages": {"kind": "all"},
            },
            {
                "kind": "string",
                "id": p_machine_id,
                "displayName": "Machine",
                "description": "",
                "variableName": "_machine",
                "selectionType": "scalar",
                "includeAllOption": True,
                "defaultValue": {"kind": "all"},
                "dataSource": {
                    "kind": "query",
                    "columns": {"value": "machine_id", "label": "machine_id"},
                    "queryRef": {"kind": "query", "queryId": q_machines_id},
                },
                "showOnPages": {"kind": "all"},
            },
        ],
        "queries": queries,
    }


# ---------------------------------------------------------------------------
# Fabric REST helpers
# ---------------------------------------------------------------------------

def wait_lro(session: requests.Session, r: requests.Response) -> requests.Response:
    if r.status_code != 202:
        return r
    op_url = r.headers.get("Operation-Location") or r.headers.get("Location")
    if not op_url:
        return r
    for _ in range(120):
        time.sleep(3)
        body = session.get(op_url).json()
        status = (body.get("status") or "").lower()
        if status == "succeeded":
            # try the /result subresource if present
            try:
                return session.get(op_url + "/result")
            except Exception:
                return r
        if status in ("failed", "cancelled"):
            raise SystemExit(f"Operation {status}: {body}")
    raise SystemExit(f"Operation timed out: {op_url}")


def b64(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return base64.b64encode(s).decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=DEFAULT_NAME)
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    load_dotenv(repo / ".env", override=True)

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]

    cred = get_credential(tenant, SCOPE, repo)
    tok = cred.get_token(SCOPE).token
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

    ws = next(w for w in s.get(f"{API}/workspaces").json()["value"]
              if w["displayName"] == ws_name)["id"]
    kdb = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"]
               if d["displayName"] == db_name)
    cluster_uri = kdb["properties"]["queryServiceUri"]
    db_id = kdb["id"]
    print(f"[ok] workspace={ws}  kqlDb={db_name} ({db_id})  cluster={cluster_uri}")

    dash_doc = build_dashboard(ws, db_id, cluster_uri, db_name, args.name)
    platform = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "KQLDashboard", "displayName": args.name, "description": ""},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    parts = [
        {"path": "RealTimeDashboard.json",
         "payload": b64(json.dumps(dash_doc)), "payloadType": "InlineBase64"},
        {"path": ".platform",
         "payload": b64(json.dumps(platform)), "payloadType": "InlineBase64"},
    ]

    # Check if already exists
    existing = [i for i in s.get(f"{API}/workspaces/{ws}/items").json()["value"]
                if i.get("type") == "KQLDashboard" and i["displayName"] == args.name]
    if existing:
        item_id = existing[0]["id"]
        print(f"[info] dashboard '{args.name}' already exists ({item_id}) — updating definition")
        r = s.post(f"{API}/workspaces/{ws}/items/{item_id}/updateDefinition?updateMetadata=true",
                   json={"definition": {"parts": parts}})
        if r.status_code not in (200, 202):
            raise SystemExit(f"updateDefinition failed: HTTP {r.status_code}: {r.text}")
        wait_lro(s, r)
    else:
        print(f"[info] creating dashboard '{args.name}'...")
        body = {
            "displayName": args.name,
            "type": "KQLDashboard",
            "definition": {"parts": parts},
        }
        r = s.post(f"{API}/workspaces/{ws}/items", json=body)
        if r.status_code not in (200, 201, 202):
            raise SystemExit(f"create item failed: HTTP {r.status_code}: {r.text}")
        wait_lro(s, r)
        # fetch id
        existing = [i for i in s.get(f"{API}/workspaces/{ws}/items").json()["value"]
                    if i.get("type") == "KQLDashboard" and i["displayName"] == args.name]
        item_id = existing[0]["id"] if existing else "(unknown)"

    print(f"[ok] dashboard '{args.name}' id={item_id}")
    print(f"[ok] open in portal: https://app.fabric.microsoft.com/groups/{ws}/dashboards/{item_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
