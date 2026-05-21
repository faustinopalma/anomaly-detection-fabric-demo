"""Factory machine telemetry simulator (physics + FSM, cloud variant).

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
    # In the cloud container this module is imported by cloud_runner.py;
    # it is not invoked directly. See ../README.md for deploy instructions.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

import numpy as np
from azure.eventhub import EventData, EventHubProducerClient
from dotenv import load_dotenv

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

    state: State = State.OFF
    state_elapsed_s: float = 0.0
    state_dwell_s: float = field(default_factory=lambda: pick_dwell(State.OFF))

    load_actual: float = 0.0
    T_motor: float = 22.0
    T_bearing: float = 22.0

    tau_load_ramp: float = 5.0
    tau_load_steady: float = 30.0
    tau_T_motor: float = 180.0
    tau_T_bearing: float = 300.0

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


@dataclass
class AnomalyOverlay:
    """Optional spike/drift/stuck overlay on a single (machine, sensor).

    Applied AFTER the physics sample() so the underlying state stays clean.
    This is the streaming-time analogue of the offline injectors in
    notebook section 8.
    """

    kind: str            # "spike" | "drift" | "stuck"
    sensor: str
    until: float
    duration: float
    stuck_value: float | None = None

    def apply(self, sample_dict: dict[str, float], now: float) -> float:
        v = sample_dict[self.sensor]
        if self.kind == "spike":
            sample_dict[self.sensor] = v + max(abs(v) * 0.5, 1.0) * random.uniform(1.5, 3.0)
            return 0.6
        if self.kind == "drift":
            t_in = 1.0 - max(0.0, self.until - now) / max(1e-6, self.duration)
            sample_dict[self.sensor] = v + t_in * max(abs(v) * 0.4, 1.0)
            return 0.7
        if self.stuck_value is None:
            self.stuck_value = v
        sample_dict[self.sensor] = self.stuck_value
        return 0.4


def maybe_trigger_overlay(now: float) -> AnomalyOverlay:
    kind = random.choice(["spike", "drift", "stuck"])
    sensor = random.choice(SENSOR_NAMES)
    if kind == "spike":
        return AnomalyOverlay(kind, sensor, now + 0.5, 0.5)
    if kind == "drift":
        d = random.uniform(8.0, 20.0)
        return AnomalyOverlay(kind, sensor, now + d, d)
    d = random.uniform(5.0, 15.0)
    return AnomalyOverlay(kind, sensor, now + d, d)


# ---------------------------------------------------------------------------
# Simulator main loop
# ---------------------------------------------------------------------------


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_machines(n_machines: int) -> dict[str, Machine]:
    machines: dict[str, Machine] = {}
    for i in range(1, n_machines + 1):
        machine_id = f"M-{i:03d}"
        m = Machine(
            machine_id=machine_id,
            nominal_rpm=3000.0 * random.uniform(0.98, 1.02),
            state=State.OFF,
        )
        machines[machine_id] = m
    return machines


def chunked(seq: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run(
    conn_str: str,
    machines: dict[str, Machine],
    rate_per_sensor: float,
    duration_s: float,
    anomaly_prob: float,
    batch_size: int,
    quiet: bool,
) -> None:
    producer = EventHubProducerClient.from_connection_string(conn_str)
    interval = 1.0 / rate_per_sensor
    deadline = time.time() + duration_s if duration_s > 0 else float("inf")
    next_tick = time.time()

    sensors_per_machine = len(SENSOR_NAMES)
    total_per_tick = len(machines) * sensors_per_machine
    if not quiet:
        print(
            f"[sim] machines={len(machines)} sensors/machine={sensors_per_machine} "
            f"rate={rate_per_sensor}/s -> {int(total_per_tick * rate_per_sensor)} events/s "
            f"duration={'inf' if duration_s <= 0 else f'{duration_s:.0f}s'} "
            f"dt={interval:.3f}s",
            flush=True,
        )

    active: dict[str, AnomalyOverlay] = {}

    sent = 0
    try:
        with producer:
            while time.time() < deadline:
                now = time.time()
                events: list[dict] = []

                for machine_id, m in machines.items():
                    m.step(interval)
                    s = m.sample()

                    ov = active.get(machine_id)
                    if ov is not None and now >= ov.until:
                        active.pop(machine_id, None)
                        ov = None
                    if ov is None and m.state != State.OFF and random.random() < anomaly_prob:
                        ov = maybe_trigger_overlay(now)
                        active[machine_id] = ov
                        # Emit a "ground truth" marker event so downstream tools can
                        # correlate the injection with the model's detection.
                        # Encoded as a fake telemetry row with a special sensor_id.
                        events.append({
                            "machine_id": machine_id,
                            "sensor_id":  f"__inject__{ov.kind}:{ov.sensor}",
                            "ts":         iso_utc(now),
                            "value":      round(float(ov.duration), 3),
                            "quality":    -1.0,
                        })

                    ov_quality = ov.apply(s, now) if ov is not None else None

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
                        f"{first_m.machine_id} state={first_m.state.value} "
                        f"load={first_m.load_actual:.2f} "
                        f"T_motor={first_m.T_motor:.1f}C",
                        flush=True,
                    )

                next_tick += interval
                sleep_for = next_tick - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.time()
    except KeyboardInterrupt:
        print("\n[sim] interrupted by user", flush=True)
    finally:
        print(f"[sim] sent {sent} events total", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--machines", type=int, default=5)
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--duration", type=float, default=0)
    p.add_argument("--anomaly-prob", type=float, default=0.0)
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--conn", type=str, default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    conn_str = args.conn or os.environ.get("EVENTSTREAM_CONNECTION_STRING")
    if not conn_str:
        print(
            "ERROR: no Eventstream connection string. Set EVENTSTREAM_CONNECTION_STRING in env "
            "or pass --conn '<connection-string>'.",
            file=sys.stderr,
        )
        return 2

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    signal.signal(signal.SIGINT, signal.default_int_handler)

    machines = build_machines(args.machines)
    run(
        conn_str=conn_str,
        machines=machines,
        rate_per_sensor=args.rate,
        duration_s=args.duration,
        anomaly_prob=args.anomaly_prob,
        batch_size=args.batch_size,
        quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
