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


@dataclass
class InjectionRequest:
    """A manual anomaly injection queued by the operator."""

    kind: str                       # one of VALID_KINDS
    sensor: str | None = None       # specific sensor, or None = pick randomly
    requested_at: float = field(default_factory=time.time)


@dataclass
class _MachineEntry:
    sensor_names: list[str]
    random_enabled: bool
    anomaly_prob: float
    queue: Deque[InjectionRequest] = field(default_factory=deque)
    # Published status (updated by the sim loop each tick)
    state: str = "unknown"
    active: bool = False
    last_sample: dict[str, float] = field(default_factory=dict)
    active_anomaly: str | None = None
    updated_at: float = 0.0


class ControlState:
    """Thread-safe per-machine control + status registry."""

    def __init__(self, default_anomaly_prob: float = 0.0) -> None:
        self._lock = threading.RLock()
        self._machines: dict[str, _MachineEntry] = {}
        self._default_prob = float(default_anomaly_prob)
        self._started_at = time.time()

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
            e.state = state
            e.active = active
            e.last_sample = {k: round(float(v), 4) for k, v in sample.items()}
            e.active_anomaly = active_anomaly
            e.updated_at = time.time()

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
                    "last_sample": e.last_sample,
                    "updated_at": e.updated_at,
                    "stale_s": round(now - e.updated_at, 1) if e.updated_at else None,
                })
            return {
                "server_time": now,
                "uptime_s": round(now - self._started_at, 1),
                "machine_count": len(self._machines),
                "machines": machines,
            }
