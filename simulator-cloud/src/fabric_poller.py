"""Optional background poller for the Fabric `anomalies` KQL table.

When enabled, a daemon thread periodically queries the Eventhouse/KQL database
for recently detected anomalies and pushes them into the shared
:class:`control.ControlState`. The control panel then marks *when the Fabric
model reacted* on the sensor charts and highlights detections that don't line
up with an actual injected anomaly (false positives).

The feature is entirely optional and degrades gracefully: if the dependencies
are missing, the container has no managed identity, or the identity lacks
read access to the KQL database, the poller logs a warning and stops without
affecting the simulator or the rest of the control plane.

Configuration (environment variables):

  SIM_FABRIC_QUERY_ENABLED   "1" to start the poller
  SIM_KUSTO_CLUSTER_URI      e.g. https://<...>.kusto.fabric.microsoft.com
  SIM_KUSTO_DATABASE         e.g. kql_telemetry
  SIM_FABRIC_POLL_INTERVAL_S poll cadence in seconds (default 15)
  SIM_FABRIC_LOOKBACK_MIN    how far back each query looks (default 10)
"""

from __future__ import annotations

import datetime as _dt
import os
import threading

from control import ControlState, Detection


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _to_epoch(value: object) -> float | None:
    """Coerce a Kusto datetime cell to epoch seconds (UTC)."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    # ISO string fallback
    try:
        s = str(value).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class FabricPoller:
    """Polls the Fabric `anomalies` table and feeds detections to ControlState."""

    def __init__(
        self,
        control: ControlState,
        *,
        cluster_uri: str,
        database: str,
        interval_s: float = 15.0,
        lookback_min: float = 10.0,
    ) -> None:
        self._control = control
        self._cluster_uri = cluster_uri.rstrip("/")
        self._database = database
        self._interval_s = max(5.0, float(interval_s))
        self._lookback_min = max(1.0, float(lookback_min))
        self._client = None
        self._stop = threading.Event()

    def _connect(self) -> None:
        """Build a Kusto client authenticated with DefaultAzureCredential.

        Imported lazily so the simulator has no hard dependency on the Kusto
        SDK when the poller is disabled.
        """
        from azure.identity import DefaultAzureCredential
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
            self._cluster_uri, credential
        )
        self._client = KustoClient(kcsb)

    def _query_once(self) -> int:
        """Run one poll. Returns the number of new detections stored."""
        assert self._client is not None
        query = (
            "anomalies "
            f"| where detected_at > ago({int(self._lookback_min)}m) "
            "| where is_anomaly == true "
            "| project detected_at, machine_id, sensor_id, model_name, score "
            "| order by detected_at asc"
        )
        resp = self._client.execute(self._database, query)
        rows = resp.primary_results[0]
        detections: list[Detection] = []
        for row in rows:
            ts = _to_epoch(row["detected_at"])
            machine_id = row["machine_id"]
            if ts is None or not machine_id:
                continue
            sensor_id = row["sensor_id"]
            detections.append(Detection(
                machine_id=str(machine_id),
                detected_at=ts,
                score=float(row["score"] or 0.0),
                model_name=str(row["model_name"] or "model"),
                sensor_id=str(sensor_id) if sensor_id else None,
            ))
        if not detections:
            return 0
        return self._control.add_detections(detections)

    def run(self) -> None:
        """Poll loop. Connects once, then queries on a fixed cadence. Transient
        query errors are logged and retried; a connection failure stops the
        poller (the feature is optional)."""
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001 — optional feature
            print(f"[fabric_poller] disabled — cannot connect to Kusto: {exc}",
                  flush=True)
            return
        print(f"[fabric_poller] polling {self._cluster_uri} db={self._database} "
              f"every {self._interval_s:.0f}s", flush=True)
        while not self._stop.is_set():
            try:
                added = self._query_once()
                if added:
                    print(f"[fabric_poller] +{added} detection(s)", flush=True)
            except Exception as exc:  # noqa: BLE001 — keep polling on transient errors
                print(f"[fabric_poller] query error: {exc}", flush=True)
            self._stop.wait(self._interval_s)

    def stop(self) -> None:
        self._stop.set()


def maybe_start_poller(control: ControlState) -> FabricPoller | None:
    """Start the Fabric anomalies poller on a daemon thread when enabled and
    configured. Returns the poller (or None when disabled/misconfigured)."""
    if not _truthy(os.environ.get("SIM_FABRIC_QUERY_ENABLED")):
        return None
    cluster_uri = os.environ.get("SIM_KUSTO_CLUSTER_URI", "").strip()
    database = os.environ.get("SIM_KUSTO_DATABASE", "").strip()
    if not cluster_uri or not database:
        print("[fabric_poller] SIM_FABRIC_QUERY_ENABLED set but "
              "SIM_KUSTO_CLUSTER_URI / SIM_KUSTO_DATABASE missing — disabled",
              flush=True)
        return None
    try:
        interval_s = float(os.environ.get("SIM_FABRIC_POLL_INTERVAL_S", "15"))
    except ValueError:
        interval_s = 15.0
    try:
        lookback_min = float(os.environ.get("SIM_FABRIC_LOOKBACK_MIN", "10"))
    except ValueError:
        lookback_min = 10.0

    poller = FabricPoller(
        control,
        cluster_uri=cluster_uri,
        database=database,
        interval_s=interval_s,
        lookback_min=lookback_min,
    )
    thread = threading.Thread(target=poller.run, name="fabric-poller", daemon=True)
    thread.start()
    return poller
