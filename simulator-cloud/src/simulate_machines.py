"""Factory machine telemetry simulator (physics + FSM).

Sends synthetic real-time measurements from N machines (each with 8 sensors)
to the Fabric Eventstream `es_machines` via its Event Hubs-compatible
custom-endpoint connection string.

The per-machine model is a port of `notebooks/01_simulator_dev.ipynb`:

* Operating-state FSM: OFF / STARTUP / IDLE / RAMP_UP / PRODUCTION_LIGHT /
  PRODUCTION_HEAVY / RAMP_DOWN / SHUTDOWN with state-specific dwell
  distributions and transition probabilities.
* Physical state with first-order dynamics: load tracks the state's
  target_load; T_motor heats with load^2 and cools toward ambient;
  T_bearing is slaved to T_motor with extra lag.
* 8 sensors derived from (load, T_motor, T_bearing) with realistic
  coupling and load-dependent jitter; all sensors emit 0.0 in OFF.

Event payload (matches docs/architecture.md and the KQL `raw_telemetry`
ProcessedIngestion column mapping):

    {
        "machine_id": "M-001",
        "sensor_id":  "temperature_motor",
        "ts":         "2026-05-07T14:23:01.123456Z",
        "value":      61.842,
        "quality":    1.0
    }

Usage
-----
    pip install -r simulator-local/requirements.txt
    # then either set EVENTSTREAM_CONNECTION_STRING in .env, or pass --conn
    python simulator-local/simulate_machines.py --machines 5 --rate 1 --duration 60

Notes
-----
* The Eventstream custom endpoint is Event Hubs compatible, so we use the
  azure-eventhub SDK directly.
* `--rate` is samples-per-second *per sensor*. Total events/s =
  machines x 8 x rate. The FSM steps once per tick with dt = 1/rate.
* `--anomaly-prob` (per machine-tick) optionally overlays a short
  spike / drift / stuck on a random sensor on top of the physics output.
  Set to 0 to disable. Real evaluation happens on the labelled offline
  eval dataset (see notebook section 8), not on live telemetry.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

import numpy as np
from azure.eventhub import EventData, EventHubProducerClient
from dotenv import load_dotenv

if TYPE_CHECKING:
    from control import ControlState

# CNC engine (machine M-003) lives next to this file; it is also copied into
# simulator-cloud/src for the container image.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cnc_engine import CncEngine, load_profile  # noqa: E402

# ---------------------------------------------------------------------------
# Operating-state FSM (ported from notebooks/01_simulator_dev.ipynb)
# ---------------------------------------------------------------------------


class State(str, Enum):
    OFF              = "OFF"
    STARTUP          = "STARTUP"
    IDLE             = "IDLE"
    PRODUCTION_LIGHT = "PRODUCTION_LIGHT"
    PRODUCTION_HEAVY = "PRODUCTION_HEAVY"
    RAMP_UP          = "RAMP_UP"
    RAMP_DOWN        = "RAMP_DOWN"
    SHUTDOWN         = "SHUTDOWN"


@dataclass
class StateSpec:
    target_load: float                       # 0..1
    dwell_s: tuple[float, float]             # (min, max) seconds in this state
    transitions: list[tuple["State", float]] # (next_state, weight)


STATE_SPECS: dict[State, StateSpec] = {
    State.OFF: StateSpec(
        target_load=0.0,
        dwell_s=(120, 600),
        transitions=[(State.STARTUP, 1.0)],
    ),
    State.STARTUP: StateSpec(
        target_load=0.10,
        dwell_s=(15, 30),
        transitions=[(State.IDLE, 1.0)],
    ),
    State.IDLE: StateSpec(
        target_load=0.10,
        dwell_s=(60, 300),
        transitions=[
            (State.RAMP_UP,  0.7),
            (State.SHUTDOWN, 0.1),
            (State.IDLE,     0.2),
        ],
    ),
    State.RAMP_UP: StateSpec(
        target_load=0.5,
        dwell_s=(10, 30),
        transitions=[
            (State.PRODUCTION_LIGHT, 0.5),
            (State.PRODUCTION_HEAVY, 0.5),
        ],
    ),
    State.PRODUCTION_LIGHT: StateSpec(
        target_load=0.40,
        dwell_s=(180, 900),
        transitions=[
            (State.RAMP_UP,          0.3),   # to HEAVY
            (State.RAMP_DOWN,        0.4),
            (State.PRODUCTION_LIGHT, 0.3),
        ],
    ),
    State.PRODUCTION_HEAVY: StateSpec(
        target_load=0.85,
        dwell_s=(120, 600),
        transitions=[
            (State.RAMP_DOWN,        0.6),
            (State.PRODUCTION_HEAVY, 0.4),
        ],
    ),
    State.RAMP_DOWN: StateSpec(
        target_load=0.20,
        dwell_s=(10, 30),
        transitions=[
            (State.IDLE,             0.6),
            (State.PRODUCTION_LIGHT, 0.4),
        ],
    ),
    State.SHUTDOWN: StateSpec(
        target_load=0.0,
        dwell_s=(15, 40),
        transitions=[(State.OFF, 1.0)],
    ),
}


def pick_next_state(current: State) -> State:
    spec = STATE_SPECS[current]
    states, weights = zip(*spec.transitions)
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    # Pick by index: np.random.choice on enum members truncates strings.
    idx = int(np.random.choice(len(states), p=w))
    return states[idx]


def pick_dwell(state: State) -> float:
    lo, hi = STATE_SPECS[state].dwell_s
    return random.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Machine physical state + sensor model
# ---------------------------------------------------------------------------

SENSOR_NAMES: tuple[str, ...] = (
    "temperature_motor",
    "temperature_bearing",
    "vibration_axial",
    "vibration_radial",
    "current",
    "spindle_rpm",
    "pressure_hydraulic",
    "power",
)


@dataclass
class Machine:
    machine_id: str
    nominal_rpm: float = 3000.0
    ambient_c: float = 22.0

    # FSM state
    state: State = State.OFF
    state_elapsed_s: float = 0.0
    state_dwell_s: float = field(default_factory=lambda: pick_dwell(State.OFF))
    forced_state: State | None = None

    # Physical state
    load_actual: float = 0.0
    T_motor: float = 22.0
    T_bearing: float = 22.0

    # Time constants (seconds)
    tau_load_ramp: float = 5.0
    tau_load_steady: float = 30.0
    tau_T_motor: float = 180.0
    tau_T_bearing: float = 300.0

    # Sensor coefficients (tunable)
    k_current_a: float = 1.0
    k_current_b: float = 14.0
    k_power_factor: float = 0.42
    k_pressure_a: float = 80.0
    k_pressure_b: float = 70.0
    k_vib_axial_base: float = 0.10
    k_vib_axial_load: float = 0.25
    k_vib_radial_base: float = 0.15
    k_vib_radial_load: float = 0.35
    k_rpm_droop: float = 0.05

    def step(self, dt: float) -> None:
        if self.forced_state is not None:
            # Operator override: pin the FSM to the requested state and keep
            # the dwell timer from ever expiring so it stays put.
            self.state = self.forced_state
            self.state_elapsed_s = 0.0
        else:
            self.state_elapsed_s += dt
            if self.state_elapsed_s >= self.state_dwell_s:
                self.state = pick_next_state(self.state)
                self.state_elapsed_s = 0.0
                self.state_dwell_s = pick_dwell(self.state)

        target = STATE_SPECS[self.state].target_load
        tau = self.tau_load_ramp if self.state in (
            State.RAMP_UP, State.RAMP_DOWN, State.STARTUP, State.SHUTDOWN
        ) else self.tau_load_steady
        alpha = 1.0 - math.exp(-dt / tau)
        self.load_actual += alpha * (target - self.load_actual)

        heat_input = 0.0 if self.state == State.OFF else 60.0 * (self.load_actual ** 2)
        T_target_motor = self.ambient_c + heat_input
        a_m = 1.0 - math.exp(-dt / self.tau_T_motor)
        self.T_motor += a_m * (T_target_motor - self.T_motor)

        T_target_bearing = self.ambient_c + 0.85 * (self.T_motor - self.ambient_c)
        a_b = 1.0 - math.exp(-dt / self.tau_T_bearing)
        self.T_bearing += a_b * (T_target_bearing - self.T_bearing)

    @property
    def sensor_names(self) -> list[str]:
        return list(SENSOR_NAMES)

    @property
    def valid_states(self) -> list[str]:
        return [s.value for s in State]

    def set_forced_state(self, state: str | None) -> None:
        """Pin the FSM to ``state`` (a State value) or None to resume auto."""
        if state is None:
            if self.forced_state is not None:
                # Resume the dwell timer from a fresh window in the held state.
                self.forced_state = None
                self.state_elapsed_s = 0.0
                self.state_dwell_s = pick_dwell(self.state)
            return
        self.forced_state = State(state)

    def is_active(self) -> bool:
        return self.state != State.OFF

    def status(self) -> str:
        return (f"state={self.state.value} load={self.load_actual:.2f} "
                f"T_motor={self.T_motor:.1f}C")

    def sample(self) -> dict[str, float]:
        if self.state == State.OFF:
            return {k: 0.0 for k in SENSOR_NAMES}

        load = max(0.0, self.load_actual)
        jitter_axial  = float(np.random.normal(0, 0.02 + 0.05 * load))
        jitter_radial = float(np.random.normal(0, 0.03 + 0.07 * load))

        rpm      = self.nominal_rpm * (1.0 - self.k_rpm_droop * load) + float(np.random.normal(0, 8))
        current  = self.k_current_a + self.k_current_b * load + float(np.random.normal(0, 0.3))
        power    = self.k_power_factor * current * (1.0 + 0.1 * load) + float(np.random.normal(0, 0.2))
        pressure = self.k_pressure_a + self.k_pressure_b * load + float(np.random.normal(0, 1.0))
        vib_a    = self.k_vib_axial_base  + self.k_vib_axial_load  * load ** 1.5 + jitter_axial
        vib_r    = self.k_vib_radial_base + self.k_vib_radial_load * load ** 1.2 + jitter_radial

        return {
            "temperature_motor":   self.T_motor   + float(np.random.normal(0, 0.4)),
            "temperature_bearing": self.T_bearing + float(np.random.normal(0, 0.3)),
            "vibration_axial":     max(0.0, vib_a),
            "vibration_radial":    max(0.0, vib_r),
            "current":             max(0.0, current),
            "spindle_rpm":         max(0.0, rpm),
            "pressure_hydraulic":  max(0.0, pressure),
            "power":               max(0.0, power),
        }


# ---------------------------------------------------------------------------
# Streaming-only post-hoc anomaly overlay
# ---------------------------------------------------------------------------

# Per-level scaling for anomaly overlays. A single global level (1..5, set by
# the operator from the control panel) drives both how *large* the deviation is
# and how *long* it lasts, so higher levels are unmistakably visible on the
# 5-minute charts. Index by level (1-based).
LEVEL_MAGNITUDE: dict[int, float] = {1: 0.6, 2: 0.85, 3: 1.0, 4: 1.35, 5: 1.8}
LEVEL_DURATION: dict[int, float] = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.7}

# Base durations (seconds) per kind, before the per-level factor. Spikes used
# to last a single tick (~0.5 s) which is invisible at 1 Hz on a 300-point
# chart; they now span several seconds so the injected band reads clearly.
BASE_DURATION: dict[str, float] = {"spike": 5.0, "drift": 14.0, "stuck": 12.0}

# Operating-scale floor for the deviation amplitude. The value-relative term
# (max(|v|·0.5,1)) is fine for the O(1–100) physics sensors but is negligible
# for sensors with a large operating spread (e.g. CNC mandrino_torque, σ≈4300)
# and collapses to ~0 while a machine is idle. We therefore floor the spike /
# drift amplitude at a multiple of the sensor's recent operating σ so injected
# windows always land a few σ off the model's learned normal manifold and the
# per-machine autoencoder reacts. σ is supplied by the run loop (rolling 120 s);
# 0 when unknown → behaviour identical to the original value-relative overlay.
# Because amplitude is a max(), this can only *increase* the injected
# deviation — it never produces false positives (those come from normal data).
SPIKE_SIGMA_K = 1.3   # spike floor ≈ 1.3·σ before the per-level factor → ~4σ @ L3
DRIFT_SIGMA_K = 1.5   # drift peak floor ≈ 1.5·σ before the per-level factor


def _level_factors(level: int) -> tuple[float, float]:
    """Return (magnitude_factor, duration_factor) for a 1..5 level."""
    lvl = max(1, min(5, int(level)))
    return LEVEL_MAGNITUDE[lvl], LEVEL_DURATION[lvl]


@dataclass
class AnomalyOverlay:
    """Optional spike/drift/stuck overlay on a single (machine, sensor).

    Applied AFTER the physics sample() so the underlying state stays clean.
    This is the streaming-time analogue of the offline injectors in
    notebook section 8; it is intentionally simple because real evaluation
    happens on the labelled eval dataset, not on live telemetry.

    ``level`` (1..5) is the operator-selected global strength: it scales both
    the deviation magnitude and the overlay duration so the effect is clearly
    visible on the live charts. ``start`` records the onset epoch so the
    control plane can shade the exact injected interval. ``scale`` is the
    sensor's recent operating σ (0 = unknown); it floors the deviation so
    large-σ / idle sensors still move a few σ off-manifold.
    """

    kind: str            # "spike" | "drift" | "stuck"
    sensor: str
    until: float
    duration: float
    level: int = 3
    start: float = 0.0
    scale: float = 0.0
    stuck_value: float | None = None

    def apply(self, sample_dict: dict[str, float], now: float) -> float:
        """Return modified quality. Mutates sample_dict[self.sensor] in place."""
        v = sample_dict[self.sensor]
        mag, _ = _level_factors(self.level)
        if self.kind == "spike":
            # Sustained elevated band for the whole overlay window, with light
            # jitter so it does not look like a flat clip. Magnitude grows with
            # the level and is floored at a multiple of the operating σ.
            amp = max(abs(v) * 0.5, 1.0, SPIKE_SIGMA_K * self.scale) * (1.5 + 1.5 * mag)
            sign = 1.0 if v >= 0 else -1.0
            sample_dict[self.sensor] = v + sign * amp * random.uniform(0.85, 1.15)
            return max(0.0, 0.6 - 0.08 * self.level)
        if self.kind == "drift":
            t_in = 1.0 - max(0.0, self.until - now) / max(1e-6, self.duration)
            amp = max(abs(v) * 0.4, 1.0, DRIFT_SIGMA_K * self.scale) * (1.0 + mag)
            sample_dict[self.sensor] = v + t_in * amp
            return max(0.0, 0.7 - 0.07 * self.level)
        # stuck: freeze the sensor. If the sensor has a known operating σ, freeze
        # at an off-manifold constant (current value + a few σ) so a stuck CNC
        # signal is detectable even when the machine state barely changes.
        if self.stuck_value is None:
            sign = 1.0 if v >= 0 else -1.0
            self.stuck_value = v + sign * SPIKE_SIGMA_K * self.scale * (1.0 + mag)
        sample_dict[self.sensor] = self.stuck_value
        return max(0.0, 0.4 - 0.05 * self.level)


# ---------------------------------------------------------------------------
# CNC empirical machine (M-003) - wraps the profile-driven CncEngine in the
# same interface as the FSM Machine so the run loop stays polymorphic.
# ---------------------------------------------------------------------------


class CNCMachine:
    """Real-data-derived CNC spindle machine (see simulator-local/cnc_engine.py)."""

    def __init__(self, machine_id: str, profile: dict, seed: int | None = None) -> None:
        self.machine_id = machine_id
        self._eng = CncEngine(profile, np.random.default_rng(seed))
        self._sensor_names = list(self._eng.sensors)

    @property
    def sensor_names(self) -> list[str]:
        return list(self._sensor_names)

    @property
    def valid_states(self) -> list[str]:
        # The CNC engine has no FSM, but the operator can still pin it to a
        # continuous machining cycle or a quiet pause for the demo.
        return ["ACTIVE", "IDLE"]

    def set_forced_state(self, state: str | None) -> None:
        # state is one of valid_states ("ACTIVE"/"IDLE") or None to resume auto.
        self._eng.set_forced_mode(state)

    def is_active(self) -> bool:
        return self._eng.active

    def step(self, dt: float) -> None:
        self._eng.step(dt)

    def sample(self) -> dict[str, float]:
        return self._eng.sample()

    def status(self) -> str:
        mode = "CUT" if self._eng.active else "idle"
        return f"mode={mode}"


# ---------------------------------------------------------------------------
# Synthgen replay machine (M-002) - replays a pre-generated synthetic CNC
# spindle trace (produced by tools/build_synth_trace.py from the synthgen
# hybrid model) on a loop, behind the same polymorphic interface. No real
# data is shipped; the trace is fully synthetic and privacy-safe.
# ---------------------------------------------------------------------------


def load_synth_trace(path: str | Path) -> dict:
    """Load and validate a synthgen replay trace JSON."""
    trace = json.loads(Path(path).read_text(encoding="utf-8"))
    values = np.asarray(trace["values"], dtype=np.float32)
    active = np.asarray(trace.get("active", np.ones(len(values))), dtype=bool)
    if values.ndim != 2 or values.shape[0] == 0:
        raise ValueError(f"synth trace {path}: expected non-empty 2-D values")
    if len(active) != len(values):
        raise ValueError(f"synth trace {path}: active/values length mismatch")
    trace["_values"] = values
    trace["_active"] = active
    return trace


class SynthMachine:
    """CNC spindle driven by a looped synthgen replay trace.

    The trace is a 1 Hz sequence of the three spindle signals plus an
    active/idle flag. ``step(dt)`` advances a fractional cursor (1 row per
    second) and the playback loops seamlessly. The operator can pin the
    machine ACTIVE (cut continuously, skipping idle rows) or IDLE (near-zero).
    """

    def __init__(self, machine_id: str, trace: dict, seed: int | None = None) -> None:
        self.machine_id = machine_id
        self._sensor_names = list(trace["sensors"])
        self._values = trace["_values"]
        self._active = trace["_active"]
        self._n = len(self._values)
        self._rng = np.random.default_rng(seed)
        # Active-only playlist for the forced-ACTIVE mode.
        self._active_idx = np.flatnonzero(self._active)
        if self._active_idx.size == 0:
            self._active_idx = np.arange(self._n)
        # Idle noise std derived from the trace's own idle rows.
        idle_rows = self._values[~self._active]
        if idle_rows.shape[0] > 1:
            self._idle_std = idle_rows.std(axis=0).astype(np.float32)
        else:
            self._idle_std = np.maximum(
                np.abs(self._values).std(axis=0) * 0.02, 1e-3
            ).astype(np.float32)
        self._cursor = 0.0       # fractional row index over the full trace
        self._acursor = 0.0      # fractional index over the active playlist
        self._forced: str | None = None

    @property
    def sensor_names(self) -> list[str]:
        return list(self._sensor_names)

    @property
    def valid_states(self) -> list[str]:
        return ["ACTIVE", "IDLE"]

    def set_forced_state(self, state: str | None) -> None:
        self._forced = state if state in ("ACTIVE", "IDLE") else None

    def is_active(self) -> bool:
        if self._forced == "ACTIVE":
            return True
        if self._forced == "IDLE":
            return False
        return bool(self._active[int(self._cursor) % self._n])

    def step(self, dt: float) -> None:
        self._cursor += dt
        self._acursor += dt

    def sample(self) -> dict[str, float]:
        if self._forced == "IDLE":
            row = self._rng.normal(0.0, self._idle_std).astype(np.float32)
        elif self._forced == "ACTIVE":
            i = self._active_idx[int(self._acursor) % self._active_idx.size]
            row = self._values[i]
        else:
            row = self._values[int(self._cursor) % self._n]
        return {name: float(row[j]) for j, name in enumerate(self._sensor_names)}

    def status(self) -> str:
        mode = "CUT" if self.is_active() else "idle"
        return f"mode={mode}"


def maybe_trigger_overlay(
    now: float, sensor_names: list[str], level: int = 3,
    scales: dict[str, float] | None = None,
) -> AnomalyOverlay:
    kind = random.choice(["spike", "drift", "stuck"])
    sensor = random.choice(sensor_names)
    _, dur_factor = _level_factors(level)
    # Light randomisation around the base duration, then scaled by the level.
    base = BASE_DURATION[kind] * random.uniform(0.85, 1.2)
    d = base * dur_factor
    scale = float((scales or {}).get(sensor, 0.0))
    return AnomalyOverlay(kind, sensor, now + d, d, level=level, start=now, scale=scale)


def manual_overlay(
    now: float, kind: str, sensor: str | None, sensor_names: list[str],
    level: int = 3, scales: dict[str, float] | None = None,
) -> AnomalyOverlay:
    """Build an overlay for an operator-requested manual injection.

    Like :func:`maybe_trigger_overlay` but with a caller-chosen ``kind`` and
    optional ``sensor`` (random when omitted). Durations are scaled by the
    global ``level`` so the injected band is clearly visible on the charts.
    """
    sensor = sensor or random.choice(sensor_names)
    kind = kind if kind in BASE_DURATION else "spike"
    _, dur_factor = _level_factors(level)
    d = BASE_DURATION[kind] * dur_factor
    scale = float((scales or {}).get(sensor, 0.0))
    return AnomalyOverlay(kind, sensor, now + d, d, level=level, start=now, scale=scale)


def sensor_sigmas(buf: "deque[dict[str, float]]") -> dict[str, float]:
    """Per-sensor operating σ over a rolling buffer of recent clean samples.

    Captures each sensor's operating spread (active + idle) so the injection
    overlay can size deviations relative to the real signal scale. Returns an
    empty dict until enough history has accumulated, so early-startup overlays
    fall back to the value-relative amplitude.
    """
    if len(buf) < 8:
        return {}
    sigmas: dict[str, float] = {}
    for sname in buf[-1].keys():
        vals = np.fromiter(
            (b[sname] for b in buf if sname in b), dtype=np.float64
        )
        if vals.size > 1:
            sigmas[sname] = float(vals.std())
    return sigmas



# ---------------------------------------------------------------------------
# Simulator main loop
# ---------------------------------------------------------------------------


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_machines(n_machines: int, cnc_profile: dict | None = None,
                   cnc_machine_id: str | None = None,
                   synth_trace: dict | None = None,
                   synth_machine_id: str | None = None) -> dict[str, object]:
    """Build the fleet.

    FSM physics machines are created for every id; if ``cnc_profile`` is given,
    the machine whose id matches ``cnc_machine_id`` (default: the last one,
    ``M-{n:03d}``) is replaced by a real-data-derived :class:`CNCMachine`. If
    ``synth_trace`` is given, the machine whose id matches ``synth_machine_id``
    is replaced by a :class:`SynthMachine` that replays the synthgen trace.
    """
    cnc_id = cnc_machine_id or (f"M-{n_machines:03d}" if cnc_profile else None)
    synth_id = synth_machine_id if synth_trace else None
    machines: dict[str, object] = {}
    for i in range(1, n_machines + 1):
        machine_id = f"M-{i:03d}"
        if synth_trace is not None and machine_id == synth_id:
            machines[machine_id] = SynthMachine(machine_id, synth_trace)
            continue
        if cnc_profile is not None and machine_id == cnc_id:
            machines[machine_id] = CNCMachine(machine_id, cnc_profile)
            continue
        # Per-machine variation in nominal RPM so the fleet looks like
        # individual units (mirrors the per-machine seed used in the notebook).
        machines[machine_id] = Machine(
            machine_id=machine_id,
            nominal_rpm=3000.0 * random.uniform(0.98, 1.02),
            state=State.OFF,
        )
    return machines


def chunked(seq: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class _NullBatch(list):
    """Drop-in for an Event Hubs batch that just collects events locally."""

    def add(self, event: object) -> None:  # noqa: D401 - mimic SDK signature
        self.append(event)


class _NullProducer:
    """No-op producer used by the offline/dry-run mode so the simulator can be
    exercised (and the control API tested) without an Event Hubs connection."""

    def __enter__(self) -> "_NullProducer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def create_batch(self) -> _NullBatch:
        return _NullBatch()

    def send_batch(self, batch: _NullBatch) -> None:
        return None


def run(
    conn_str: str | None,
    machines: dict[str, object],
    rate_per_sensor: float,
    duration_s: float,
    anomaly_prob: float,
    batch_size: int,
    quiet: bool,
    control: "ControlState | None" = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        producer = _NullProducer()
        if not quiet:
            print("[sim] DRY-RUN: events are generated but not sent to Event Hubs")
    else:
        producer = EventHubProducerClient.from_connection_string(conn_str)
    interval = 1.0 / rate_per_sensor          # FSM dt
    deadline = time.time() + duration_s if duration_s > 0 else float("inf")
    next_tick = time.time()

    sensors_total = sum(len(m.sensor_names) for m in machines.values())
    total_per_tick = sensors_total
    if not quiet:
        print(
            f"[sim] machines={len(machines)} sensors_total={sensors_total} "
            f"rate={rate_per_sensor}/s -> {int(total_per_tick * rate_per_sensor)} events/s "
            f"duration={'inf' if duration_s <= 0 else f'{duration_s:.0f}s'} "
            f"dt={interval:.3f}s"
        )

    # Active per-machine anomaly overlays (at most one per machine at a time)
    active: dict[str, AnomalyOverlay] = {}

    # Rolling per-machine buffer of recent CLEAN samples (pre-overlay), used to
    # size injection deviations relative to each sensor's operating σ. ~120 s.
    sigma_window = max(16, int(round(rate_per_sensor * 120)))
    recent: dict[str, deque] = {
        mid: deque(maxlen=sigma_window) for mid in machines
    }

    sent = 0
    try:
        with producer:
            while time.time() < deadline:
                now = time.time()
                events: list[dict] = []

                for machine_id, m in machines.items():
                    if control is not None:
                        m.set_forced_state(control.forced_state(machine_id))
                    m.step(interval)
                    s = m.sample()

                    # Record the clean (pre-overlay) sample so injection
                    # deviations can be sized to each sensor's operating σ.
                    buf = recent[machine_id]
                    buf.append(dict(s))

                    ov = active.get(machine_id)
                    if ov is not None and now >= ov.until:
                        active.pop(machine_id, None)
                        ov = None

                    # Per-machine random probability when an operator control
                    # plane is attached; otherwise the global CLI/env value.
                    prob = (
                        control.effective_anomaly_prob(machine_id)
                        if control is not None
                        else anomaly_prob
                    )

                    # Operator-requested manual injection takes priority over
                    # the random overlay and fires regardless of machine state.
                    ov_started = False
                    ov_source = "random"
                    level = control.get_level() if control is not None else 3
                    scales = sensor_sigmas(buf)
                    if ov is None and control is not None:
                        req = control.pop_injection(machine_id)
                        if req is not None:
                            ov = manual_overlay(
                                now, req.kind, req.sensor, m.sensor_names, level,
                                scales,
                            )
                            active[machine_id] = ov
                            ov_started = True
                            ov_source = "manual"

                    if ov is None and m.is_active() and random.random() < prob:
                        ov = maybe_trigger_overlay(now, m.sensor_names, level, scales)
                        active[machine_id] = ov
                        ov_started = True
                        ov_source = "random"

                    if ov is not None:
                        ov_quality = ov.apply(s, now)
                    else:
                        ov_quality = None

                    if control is not None:
                        st = getattr(m, "state", None)
                        state_label = (
                            st.value if st is not None
                            else ("active" if m.is_active() else "idle")
                        )
                        control.update_status(
                            machine_id,
                            state=state_label,
                            active=m.is_active(),
                            sample=s,
                            active_anomaly=ov.kind if ov is not None else None,
                        )

                    ts_iso = iso_utc(now)
                    for sensor_id, value in s.items():
                        q = ov_quality if (ov is not None and sensor_id == ov.sensor) else 1.0
                        events.append({
                            "machine_id": machine_id,
                            "sensor_id":  sensor_id,
                            "ts":         ts_iso,
                            "value":      round(float(value), 4),
                            "quality":    q,
                        })

                    # Ground-truth marker: emit one synthetic row at overlay
                    # onset (random or operator-requested) so KQL
                    # (fn_extract_injections) can populate injected_anomalies
                    # and compute per-machine quality metrics. Encoded as
                    # sensor_id=__inject__<kind>:<sensor>, value=duration_s,
                    # quality=-1.0. Applies to every machine in the fleet.
                    if ov_started:
                        events.append({
                            "machine_id": machine_id,
                            "sensor_id":  f"__inject__{ov.kind}:{ov.sensor}",
                            "ts":         ts_iso,
                            "value":      round(float(ov.duration), 4),
                            "quality":    -1.0,
                        })
                        # Record the injected interval so the control panel can
                        # shade the affected period on the sensor charts.
                        if control is not None:
                            control.record_injection(
                                machine_id,
                                kind=ov.kind,
                                sensor=ov.sensor,
                                start=ov.start or now,
                                end=ov.until,
                                level=ov.level,
                                source=ov_source,
                            )

                for chunk in chunked(events, batch_size):
                    batch = producer.create_batch()
                    for ev in chunk:
                        batch.add(EventData(json.dumps(ev)))
                    producer.send_batch(batch)
                    sent += len(chunk)

                if not quiet:
                    first_m = next(iter(machines.values()))
                    print(
                        f"[sim] +{len(events):4d} ev (total {sent:>7d})  "
                        f"{first_m.machine_id} {first_m.status()}"
                    )

                next_tick += interval
                sleep_for = next_tick - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # Falling behind; reset cadence to avoid drift accumulation.
                    next_tick = time.time()
    except KeyboardInterrupt:
        print("\n[sim] interrupted by user")
    finally:
        print(f"[sim] sent {sent} events total")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--machines", type=int, default=2, help="Number of machines to simulate (default 2)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="Samples per second per sensor (default 1.0). FSM dt = 1/rate.")
    p.add_argument("--duration", type=float, default=0,
                   help="Run duration in seconds. 0 = run forever until Ctrl-C (default 0).")
    p.add_argument("--anomaly-prob", type=float, default=0.0,
                   help="Per-machine-tick probability of triggering a streaming overlay "
                        "(spike/drift/stuck) on a random sensor (default 0 = disabled). "
                        "Use the labelled eval dataset for real evaluation.")
    p.add_argument("--batch-size", type=int, default=200,
                   help="Max events per Event Hubs batch (default 200).")
    p.add_argument("--conn", type=str, default=None,
                   help="Eventstream Event-Hub-compatible connection string. "
                        "Defaults to env var EVENTSTREAM_CONNECTION_STRING.")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
    p.add_argument("--cnc-profile", type=str, default=None,
                   help="Path to a CNC profile JSON (e.g. data/cnc_profile_M-003.json). "
                        "When set, one machine is driven by the real-data-derived CNC "
                        "engine instead of the FSM physics model.")
    p.add_argument("--cnc-machine-id", type=str, default=None,
                   help="Machine id driven by the CNC profile (e.g. M-003). Defaults to "
                        "the last machine (M-{machines:03d}). Use this to keep the CNC "
                        "engine on a fixed id while adding more physics machines.")
    p.add_argument("--synth-trace", type=str, default=None,
                   help="Path to a synthgen replay trace JSON (see "
                        "tools/build_synth_trace.py). When set, one machine replays "
                        "the synthetic CNC spindle trace on a loop.")
    p.add_argument("--synth-machine-id", type=str, default=None,
                   help="Machine id driven by the synthgen replay trace (e.g. M-002).")
    p.add_argument("--quiet", action="store_true", help="Suppress per-tick log output.")
    p.add_argument("--dry-run", action="store_true",
                   help="Generate telemetry but do not send it to Event Hubs "
                        "(no connection string required). Useful for local "
                        "testing of the control API / Static Web App.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, control: "ControlState | None" = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    conn_str = args.conn or os.environ.get("EVENTSTREAM_CONNECTION_STRING")
    dry_run = args.dry_run or os.environ.get("SIM_DRY_RUN", "").lower() in ("1", "true", "yes")
    if not conn_str and not dry_run:
        print(
            "ERROR: no Eventstream connection string. Set EVENTSTREAM_CONNECTION_STRING in .env "
            "or pass --conn '<connection-string>'.\n"
            "Get it from the Fabric portal: open Eventstream `es_machines` -> add a Custom App "
            "source -> on its Details pane copy 'Connection string-primary key'.",
            file=sys.stderr,
        )
        return 2

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except ValueError:
        # signal.signal only works in the main thread; when the simulator is
        # driven from a worker thread (e.g. local tests) just skip it.
        pass

    cnc_profile = None
    cnc_machine_id = args.cnc_machine_id or os.environ.get("SIM_CNC_MACHINE")
    cnc_path = args.cnc_profile or os.environ.get("SIM_CNC_PROFILE")
    if cnc_path:
        cnc_path = Path(cnc_path)
        if not cnc_path.is_absolute():
            cnc_path = repo_root / cnc_path
        if not cnc_path.exists():
            print(f"ERROR: CNC profile not found: {cnc_path}", file=sys.stderr)
            return 2
        cnc_profile = load_profile(cnc_path)
        cnc_target = cnc_machine_id or f"M-{args.machines:03d}"
        print(f"[sim] CNC profile loaded for {cnc_target}: {cnc_path.name}")

    synth_trace = None
    synth_machine_id = args.synth_machine_id or os.environ.get("SIM_SYNTH_MACHINE")
    synth_path = args.synth_trace or os.environ.get("SIM_SYNTH_PROFILE")
    if synth_path:
        synth_path = Path(synth_path)
        if not synth_path.is_absolute():
            synth_path = repo_root / synth_path
        if not synth_path.exists():
            print(f"ERROR: synth trace not found: {synth_path}", file=sys.stderr)
            return 2
        synth_trace = load_synth_trace(synth_path)
        synth_target = synth_machine_id or "M-002"
        print(f"[sim] synthgen trace loaded for {synth_target}: {synth_path.name} "
              f"({len(synth_trace['_values'])} steps, "
              f"duty={synth_trace.get('duty_cycle', 'n/a')})")

    machines = build_machines(
        args.machines, cnc_profile=cnc_profile, cnc_machine_id=cnc_machine_id,
        synth_trace=synth_trace, synth_machine_id=synth_machine_id,
    )
    if control is not None:
        control.register_machines(machines)
    run(
        conn_str=conn_str,
        machines=machines,
        rate_per_sensor=args.rate,
        duration_s=args.duration,
        anomaly_prob=args.anomaly_prob,
        batch_size=args.batch_size,
        quiet=args.quiet,
        control=control,
        dry_run=dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
