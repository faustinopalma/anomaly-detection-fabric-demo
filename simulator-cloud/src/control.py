"""Shared, thread-safe control state for the simulator.

The simulator main loop runs in the main thread; an optional FastAPI
control server (see ``server.py``) runs in a background thread and mutates
this state in response to operator commands from the Static Web App.

`ControlState` is the only object shared across the two threads, so every
public method takes the internal lock. The loop calls:

* :meth:`register_machines` once at start-up,
* :meth:`effective_anomaly_prob` and :meth:`pop_injection` each tick,
* :meth:`update_status` each tick to publish a snapshot for the API.

The API server calls :meth:`snapshot`, :meth:`set_random` and
:meth:`request_injection`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


VALID_KINDS = ("spike", "drift", "stuck")

# Anomaly strength levels selectable by the operator (1 = mild, 5 = severe).
# A single global level applies to every machine; the simulator loop reads it
# when it builds an overlay (manual or random).
MIN_LEVEL = 1
MAX_LEVEL = 5
DEFAULT_LEVEL = 3


def clamp_level(level: int) -> int:
    """Coerce ``level`` into the supported 1..5 range."""
    try:
        v = int(level)
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, v))


@dataclass
class InjectionRequest:
    """A manual anomaly injection queued by the operator."""

    kind: str                       # one of VALID_KINDS
    sensor: str | None = None       # specific sensor, or None = pick randomly
    requested_at: float = field(default_factory=time.time)


@dataclass
class InjectionWindow:
    """A recorded anomaly-injection interval, surfaced to the control panel so
    it can shade the affected period on the sensor charts."""

    id: int
    machine_id: str
    kind: str
    sensor: str
    start: float                    # epoch seconds (server clock)
    end: float                      # epoch seconds (server clock)
    level: int
    source: str                     # "manual" | "random"


@dataclass
class Detection:
    """An anomaly the Fabric model wrote to the `anomalies` KQL table, pulled
    back so the panel can mark *when the detector reacted*."""

    machine_id: str
    detected_at: float              # epoch seconds (UTC)
    score: float
    model_name: str
    sensor_id: str | None = None


@dataclass
class _MachineEntry:
    sensor_names: list[str]
    random_enabled: bool
    anomaly_prob: float
    valid_states: list[str] = field(default_factory=list)
    forced_state: str | None = None
    queue: Deque[InjectionRequest] = field(default_factory=deque)
    # Recorded injection windows (manual + random), pruned to the history
    # window so the panel can shade the affected period on the charts.
    injections: Deque[InjectionWindow] = field(default_factory=deque)
    # Published status (updated by the sim loop each tick)
    state: str = "unknown"
    active: bool = False
    last_sample: dict[str, float] = field(default_factory=dict)
    active_anomaly: str | None = None
    updated_at: float = 0.0
    # Rolling per-sensor history at the simulator tick rate, so the control
    # panel can render continuous charts and backfill the whole window after
    # a client disconnect. Each item is (epoch_seconds, {sensor: value}).
    history: Deque[tuple[float, dict[str, float]]] = field(default_factory=deque)


class ControlState:
    """Thread-safe per-machine control + status registry."""

    def __init__(
        self,
        default_anomaly_prob: float = 0.0,
        *,
        history_window_s: float = 300.0,
    ) -> None:
        self._lock = threading.RLock()
        self._machines: dict[str, _MachineEntry] = {}
        self._default_prob = float(default_anomaly_prob)
        self._history_window_s = float(history_window_s)
        self._started_at = time.time()
        # Global anomaly-strength level (1..5), applied to every machine.
        self._level = DEFAULT_LEVEL
        # Monotonic id generator for injection windows.
        self._next_injection_id = 1
        # Detections pulled back from the Fabric `anomalies` table, newest last.
        self._detections: Deque[Detection] = deque()

    # -- loop-side API ----------------------------------------------------

    def register_machines(self, machines: dict[str, object]) -> None:
        """Initialise one entry per machine. ``random_enabled`` defaults to
        True when the global anomaly probability is > 0."""
        with self._lock:
            for machine_id, m in machines.items():
                self._machines[machine_id] = _MachineEntry(
                    sensor_names=list(getattr(m, "sensor_names", [])),
                    random_enabled=self._default_prob > 0.0,
                    anomaly_prob=self._default_prob,
                    valid_states=list(getattr(m, "valid_states", [])),
                )

    def effective_anomaly_prob(self, machine_id: str) -> float:
        """Per-tick random-overlay probability for this machine (0 when the
        operator disabled random anomalies)."""
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None or not e.random_enabled:
                return 0.0
            return e.anomaly_prob

    def pop_injection(self, machine_id: str) -> InjectionRequest | None:
        """Return and remove the next queued manual injection, if any."""
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None or not e.queue:
                return None
            return e.queue.popleft()

    def forced_state(self, machine_id: str) -> str | None:
        """Operator-forced FSM state for this machine, or None when the machine
        should follow its own state machine (auto)."""
        with self._lock:
            e = self._machines.get(machine_id)
            return e.forced_state if e is not None else None

    def get_level(self) -> int:
        """Current global anomaly-strength level (1..5)."""
        with self._lock:
            return self._level

    def record_injection(
        self,
        machine_id: str,
        *,
        kind: str,
        sensor: str,
        start: float,
        end: float,
        level: int,
        source: str,
    ) -> None:
        """Register an injection interval so the panel can shade it on the
        charts. Called by the sim loop the moment an overlay starts."""
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None:
                return
            e.injections.append(InjectionWindow(
                id=self._next_injection_id,
                machine_id=machine_id,
                kind=kind,
                sensor=sensor,
                start=start,
                end=end,
                level=int(level),
                source=source,
            ))
            self._next_injection_id += 1
            cutoff = time.time() - self._history_window_s
            while e.injections and e.injections[0].end < cutoff:
                e.injections.popleft()

    def update_status(
        self,
        machine_id: str,
        *,
        state: str,
        active: bool,
        sample: dict[str, float],
        active_anomaly: str | None,
    ) -> None:
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None:
                return
            now = time.time()
            e.state = state
            e.active = active
            e.last_sample = {k: round(float(v), 4) for k, v in sample.items()}
            e.active_anomaly = active_anomaly
            e.updated_at = now
            # Append to the rolling history and drop anything older than the
            # window. e.last_sample is a fresh dict each tick, so storing the
            # reference is safe.
            e.history.append((now, e.last_sample))
            cutoff = now - self._history_window_s
            while e.history and e.history[0][0] < cutoff:
                e.history.popleft()

    # -- server-side API --------------------------------------------------

    def set_random(self, machine_id: str, enabled: bool) -> bool:
        """Enable/disable random anomalies for a machine. Returns False if the
        machine is unknown."""
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None:
                return False
            e.random_enabled = bool(enabled)
            return True

    def set_forced_state(self, machine_id: str, state: str | None) -> bool:
        """Force the machine into ``state`` (one of its ``valid_states``), or
        pass None to return it to automatic FSM control. Returns False if the
        machine is unknown. Raises ValueError on an unsupported state."""
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None:
                return False
            if state is not None:
                if not e.valid_states:
                    raise ValueError(
                        f"{machine_id} does not support forcing a state"
                    )
                if state not in e.valid_states:
                    raise ValueError(
                        f"unknown state {state!r} for {machine_id}; "
                        f"valid: {e.valid_states}"
                    )
            e.forced_state = state
            return True

    def request_injection(
        self, machine_id: str, kind: str, sensor: str | None = None
    ) -> bool:
        """Queue a manual injection. Returns False if the machine is unknown.
        Raises ValueError on an invalid kind/sensor."""
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind {kind!r}; expected one of {VALID_KINDS}")
        with self._lock:
            e = self._machines.get(machine_id)
            if e is None:
                return False
            if sensor is not None and sensor not in e.sensor_names:
                raise ValueError(
                    f"unknown sensor {sensor!r} for {machine_id}; "
                    f"valid: {e.sensor_names}"
                )
            e.queue.append(InjectionRequest(kind=kind, sensor=sensor))
            return True

    def set_level(self, level: int) -> int:
        """Set the global anomaly-strength level (clamped to 1..5). Returns the
        effective level actually stored."""
        with self._lock:
            self._level = clamp_level(level)
            return self._level

    def add_detections(self, detections: list[Detection]) -> int:
        """Merge detections pulled from the Fabric `anomalies` table. Dedupes on
        (machine_id, detected_at, model_name) and prunes to the history window.
        Returns the number of new detections stored."""
        with self._lock:
            existing = {
                (d.machine_id, round(d.detected_at, 3), d.model_name)
                for d in self._detections
            }
            added = 0
            for d in detections:
                key = (d.machine_id, round(d.detected_at, 3), d.model_name)
                if key in existing:
                    continue
                existing.add(key)
                self._detections.append(d)
                added += 1
            # Keep newest-last ordering and prune to the retention window.
            self._detections = deque(
                sorted(self._detections, key=lambda d: d.detected_at)
            )
            cutoff = time.time() - self._history_window_s
            while self._detections and self._detections[0].detected_at < cutoff:
                self._detections.popleft()
            return added

    def snapshot(self) -> dict:
        """Serializable view of the whole fleet for ``GET /api/state``."""
        with self._lock:
            now = time.time()
            machines = []
            for machine_id, e in sorted(self._machines.items()):
                machines.append({
                    "machine_id": machine_id,
                    "state": e.state,
                    "active": e.active,
                    "random_enabled": e.random_enabled,
                    "anomaly_prob": e.anomaly_prob,
                    "active_anomaly": e.active_anomaly,
                    "pending_injections": len(e.queue),
                    "sensors": e.sensor_names,
                    "valid_states": e.valid_states,
                    "forced_state": e.forced_state,
                    "last_sample": e.last_sample,
                    "updated_at": e.updated_at,
                    "stale_s": round(now - e.updated_at, 1) if e.updated_at else None,
                })
            return {
                "server_time": now,
                "uptime_s": round(now - self._started_at, 1),
                "machine_count": len(self._machines),
                "level": self._level,
                "machines": machines,
            }

    def history(self, since: float = 0.0) -> dict:
        """Columnar per-sensor history for ``GET /api/history``.

        Returns every stored sample with ``epoch_seconds > since`` so the
        client can fetch incrementally (``since`` = last timestamp it holds)
        or backfill the whole window after a disconnect (``since`` = 0).
        """
        with self._lock:
            now = time.time()
            machines = []
            for machine_id, e in sorted(self._machines.items()):
                ts_list: list[float] = []
                series: dict[str, list[float | None]] = {n: [] for n in e.sensor_names}
                for ts, sample in e.history:
                    if ts <= since:
                        continue
                    ts_list.append(round(ts, 3))
                    for n in e.sensor_names:
                        v = sample.get(n)
                        series[n].append(None if v is None else float(v))
                machines.append({
                    "machine_id": machine_id,
                    "t": ts_list,
                    "series": series,
                })
            return {
                "server_time": now,
                "window_s": self._history_window_s,
                "machines": machines,
            }

    def events(self) -> dict:
        """Injection windows + Fabric detections for ``GET /api/events``.

        The data volume is tiny (a handful of intervals/markers within the
        retention window), so the whole set is returned each poll. The client
        aligns the epoch timestamps to its local clock via ``server_time`` (the
        same trick the history endpoint uses) and renders shaded bands for the
        injection windows and markers for the detections.
        """
        with self._lock:
            now = time.time()
            machines = []
            for machine_id, e in sorted(self._machines.items()):
                machines.append({
                    "machine_id": machine_id,
                    "injections": [
                        {
                            "id": w.id,
                            "kind": w.kind,
                            "sensor": w.sensor,
                            "start": round(w.start, 3),
                            "end": round(w.end, 3),
                            "level": w.level,
                            "source": w.source,
                        }
                        for w in e.injections
                    ],
                })
            detections = [
                {
                    "machine_id": d.machine_id,
                    "t": round(d.detected_at, 3),
                    "score": round(float(d.score), 4),
                    "model_name": d.model_name,
                    "sensor_id": d.sensor_id,
                }
                for d in self._detections
            ]
            return {
                "server_time": now,
                "window_s": self._history_window_s,
                "level": self._level,
                "machines": machines,
                "detections": detections,
            }

