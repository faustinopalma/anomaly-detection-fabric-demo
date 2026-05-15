"""Factory machine telemetry simulator.

Sends synthetic real-time measurements from N machines (each with multiple
sensors) to the Fabric Eventstream `es_machines` via its Event Hubs-compatible
custom-endpoint connection string.

Event payload (matches docs/architecture.md and the KQL `raw_telemetry` table):

    {
        "machineId": "M-001",
        "sensorId":  "temperature_motor",
        "ts":        "2026-05-07T14:23:01.123456Z",
        "value":     61.842,
        "quality":   1.0
    }

Usage
-----
    pip install -r simulator-local/requirements.txt
    # then either set EVENTSTREAM_CONNECTION_STRING in .env, or pass --conn
    python simulator-local/simulate_machines.py --machines 5 --rate 2 --duration 60

Notes
-----
* The Eventstream custom endpoint is Event Hubs compatible, so we use the
  azure-eventhub SDK directly.
* `--rate` is samples-per-second *per sensor*. Total events/s = machines x
  sensors_per_machine x rate.
* Anomalies are injected with probability `--anomaly-prob` per sample as
  short bursts (spike, drift, or stuck value) on a random sensor.
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
from pathlib import Path
from typing import Iterable

from azure.eventhub import EventData, EventHubProducerClient
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Sensor catalogue: realistic ranges for a CNC / press / motor-driven machine
# ---------------------------------------------------------------------------

@dataclass
class SensorSpec:
    sensor_id: str
    unit: str
    baseline: float          # nominal value
    noise_std: float         # gaussian noise stddev
    seasonal_amp: float = 0  # sinusoidal component amplitude
    seasonal_period_s: float = 60.0
    drift_per_hour: float = 0.0
    min_value: float = -math.inf
    max_value: float = math.inf


SENSOR_CATALOG: list[SensorSpec] = [
    SensorSpec("temperature_motor",     "C",   60.0, 0.4, seasonal_amp=2.0,  seasonal_period_s=300, drift_per_hour=0.05),
    SensorSpec("temperature_bearing",   "C",   55.0, 0.3, seasonal_amp=1.5,  seasonal_period_s=300, drift_per_hour=0.04),
    SensorSpec("vibration_axial",       "g",    0.20, 0.02, seasonal_amp=0.05, seasonal_period_s=12),
    SensorSpec("vibration_radial",      "g",    0.30, 0.03, seasonal_amp=0.07, seasonal_period_s=12),
    SensorSpec("current",               "A",   12.0, 0.3, seasonal_amp=1.0,  seasonal_period_s=30),
    SensorSpec("spindle_rpm",           "rpm", 3000.0, 8.0, seasonal_amp=20.0, seasonal_period_s=45, min_value=0),
    SensorSpec("pressure_hydraulic",    "bar", 120.0, 1.0, seasonal_amp=3.0,  seasonal_period_s=20, min_value=0),
    SensorSpec("power",                 "kW",   8.0, 0.2, seasonal_amp=0.5,  seasonal_period_s=30, min_value=0),
]


# ---------------------------------------------------------------------------
# Per (machine, sensor) state
# ---------------------------------------------------------------------------

@dataclass
class SensorState:
    spec: SensorSpec
    started_at: float = field(default_factory=time.time)
    drift_offset: float = 0.0
    # Active anomaly:
    anomaly_kind: str | None = None       # "spike" | "drift" | "stuck"
    anomaly_until: float = 0.0
    anomaly_param: float = 0.0
    stuck_value: float | None = None

    def sample(self, now: float) -> tuple[float, float]:
        """Return (value, quality) for the current time."""
        s = self.spec
        elapsed = now - self.started_at

        # Long-term drift
        self.drift_offset = s.drift_per_hour * (elapsed / 3600.0)

        # Seasonal + noise + drift
        seasonal = (
            s.seasonal_amp * math.sin(2 * math.pi * elapsed / s.seasonal_period_s)
            if s.seasonal_amp
            else 0.0
        )
        value = s.baseline + seasonal + self.drift_offset + random.gauss(0, s.noise_std)

        # Apply active anomaly
        quality = 1.0
        if self.anomaly_kind and now < self.anomaly_until:
            if self.anomaly_kind == "spike":
                value += self.anomaly_param * s.noise_std * 15
                quality = 0.6
            elif self.anomaly_kind == "drift":
                # progressive offset that grows with time inside the window
                t_in = 1.0 - max(0.0, self.anomaly_until - now) / max(1e-6, self.anomaly_param)
                value += t_in * s.noise_std * 25
                quality = 0.7
            elif self.anomaly_kind == "stuck":
                if self.stuck_value is None:
                    self.stuck_value = value
                value = self.stuck_value
                quality = 0.4
        elif self.anomaly_kind:
            self.anomaly_kind = None
            self.stuck_value = None

        # Clamp to physical bounds
        value = max(s.min_value, min(s.max_value, value))
        return value, quality

    def trigger_anomaly(self, now: float) -> None:
        kind = random.choice(["spike", "drift", "stuck"])
        self.anomaly_kind = kind
        if kind == "spike":
            self.anomaly_until = now + 0.5  # half-second spike
            self.anomaly_param = random.uniform(1.0, 1.5)
        elif kind == "drift":
            duration = random.uniform(8.0, 20.0)
            self.anomaly_until = now + duration
            self.anomaly_param = duration
        else:  # stuck
            self.anomaly_until = now + random.uniform(5.0, 15.0)
            self.stuck_value = None


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_machines(n_machines: int) -> dict[str, dict[str, SensorState]]:
    machines: dict[str, dict[str, SensorState]] = {}
    for i in range(1, n_machines + 1):
        machine_id = f"M-{i:03d}"
        # Each machine gets all sensors, with slightly randomised baselines
        machines[machine_id] = {
            spec.sensor_id: SensorState(
                spec=SensorSpec(
                    sensor_id=spec.sensor_id,
                    unit=spec.unit,
                    baseline=spec.baseline * random.uniform(0.97, 1.03),
                    noise_std=spec.noise_std,
                    seasonal_amp=spec.seasonal_amp,
                    seasonal_period_s=spec.seasonal_period_s,
                    drift_per_hour=spec.drift_per_hour,
                    min_value=spec.min_value,
                    max_value=spec.max_value,
                )
            )
            for spec in SENSOR_CATALOG
        }
    return machines


def make_event(machine_id: str, state: SensorState, now: float) -> dict:
    value, quality = state.sample(now)
    return {
        "machineId": machine_id,
        "sensorId":  state.spec.sensor_id,
        "ts":        iso_utc(now),
        "value":     round(value, 4),
        "quality":   quality,
    }


def chunked(seq: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def run(
    conn_str: str,
    machines: dict[str, dict[str, SensorState]],
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

    sensors_per_machine = len(SENSOR_CATALOG)
    total_per_tick = len(machines) * sensors_per_machine
    if not quiet:
        print(
            f"[sim] machines={len(machines)} sensors/machine={sensors_per_machine} "
            f"rate={rate_per_sensor}/s -> {int(total_per_tick * rate_per_sensor)} events/s "
            f"duration={'inf' if duration_s <= 0 else f'{duration_s:.0f}s'}"
        )

    sent = 0
    try:
        with producer:
            while time.time() < deadline:
                now = time.time()
                events: list[dict] = []
                for machine_id, sensors in machines.items():
                    for state in sensors.values():
                        if random.random() < anomaly_prob and state.anomaly_kind is None:
                            state.trigger_anomaly(now)
                        events.append(make_event(machine_id, state, now))

                for chunk in chunked(events, batch_size):
                    batch = producer.create_batch()
                    for ev in chunk:
                        batch.add(EventData(json.dumps(ev)))
                    producer.send_batch(batch)
                    sent += len(chunk)

                if not quiet:
                    sample = events[0]
                    print(
                        f"[sim] +{len(events):4d} ev (total {sent:>7d})  "
                        f"sample: {sample['machineId']} {sample['sensorId']}={sample['value']}"
                    )

                next_tick += interval
                sleep_for = next_tick - time.time()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # We're falling behind; reset the cadence to avoid drift accumulation.
                    next_tick = time.time()
    except KeyboardInterrupt:
        print("\n[sim] interrupted by user")
    finally:
        print(f"[sim] sent {sent} events total")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--machines", type=int, default=5, help="Number of machines to simulate (default 5)")
    p.add_argument("--rate", type=float, default=1.0,
                   help="Samples per second per sensor (default 1.0). Total eps = machines x sensors x rate.")
    p.add_argument("--duration", type=float, default=0,
                   help="Run duration in seconds. 0 = run forever until Ctrl-C (default 0).")
    p.add_argument("--anomaly-prob", type=float, default=0.0005,
                   help="Per-sample probability of triggering an anomaly on each sensor (default 0.0005).")
    p.add_argument("--batch-size", type=int, default=200,
                   help="Max events per Event Hubs batch (default 200).")
    p.add_argument("--conn", type=str, default=None,
                   help="Eventstream Event-Hub-compatible connection string. "
                        "Defaults to env var EVENTSTREAM_CONNECTION_STRING.")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
    p.add_argument("--quiet", action="store_true", help="Suppress per-tick log output.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Load .env from repo root regardless of CWD
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    conn_str = args.conn or os.environ.get("EVENTSTREAM_CONNECTION_STRING")
    if not conn_str:
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

    # Make Ctrl-C feel snappy on Windows
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
