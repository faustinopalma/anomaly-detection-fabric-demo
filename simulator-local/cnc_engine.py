"""Profile-driven CNC spindle telemetry engine (shared by simulator + trainer).

This is the behavioural model for machine **M-003**, a real CNC machining
engine cylinder heads. Unlike the FSM physics model used by M-001/M-002, the
CNC engine is *empirical*: it replays the statistical regime extracted from the
real customer telemetry (see ``tools/cnc_build_profile.py`` ->
``data/cnc_profile_M-003.json``).

Behaviour
---------
* The machine alternates between **active** machining cycles (~43 s) and
  **idle** part-to-part pauses (log-normal gaps, capped for the live demo),
  reproducing the real ~10 % duty cycle.
* Inside a cycle it steps through the observed machining phases
  ``[1, 2, 3, 4, 21, 22, 23, 24]`` in order, spending in each a fraction of the
  cycle equal to its real occupancy share.
* Each of the three measured signals (``mandrino_load`` %, ``mandrino_power``
  kW, ``mandrino_torque`` N*cm) is drawn from its per-phase mean/std and
  smoothed with the per-signal AR(1) coefficient, clamped to the observed
  range. During idle the signals sit near zero (machine not cutting).

The same engine is used to (a) drive live telemetry in the simulator and
(b) generate the training frame for the M-003 anomaly model, guaranteeing
train/serve consistency. It depends only on numpy.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def load_profile(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


class CncEngine:
    """Stateful per-machine generator driven by a CNC profile dict."""

    def __init__(self, profile: dict, rng: np.random.Generator | None = None) -> None:
        self.profile = profile
        self.rng = rng if rng is not None else np.random.default_rng()
        self.sensors: list[str] = list(profile["sensors"])

        self.phases: list[int] = [int(p) for p in profile["phases"]]
        share = profile["phase_time_share"]
        total = sum(share[str(p)] for p in self.phases)
        self.phase_share = np.array([share[str(p)] / total for p in self.phases], dtype=float)

        stats = profile["phase_signal_stats"]
        # Per (phase, sensor): mean, std, min, max as parallel arrays.
        self.mean = np.array([[stats[str(p)][s]["mean"] for s in self.sensors] for p in self.phases])
        self.std = np.array([[stats[str(p)][s]["std"] for s in self.sensors] for p in self.phases])
        self.lo = np.array([[stats[str(p)][s]["min"] for s in self.sensors] for p in self.phases])
        self.hi = np.array([[stats[str(p)][s]["max"] for s in self.sensors] for p in self.phases])

        self.ar1 = np.array([profile["ar1"][s] for s in self.sensors], dtype=float)
        self.idle_std = np.array([profile["idle_signal"][s]["std"] for s in self.sensors], dtype=float)

        self._cycle = profile["cycle"]
        self._idle = profile["idle"]

        # Runtime state
        self.active = False
        self.forced_mode: str | None = None  # None | "ACTIVE" | "IDLE"
        self._mode_remaining = 0.0          # seconds left in current idle pause
        self._phase_i = 0                   # index into self.phases
        self._phase_remaining = 0.0
        self._phase_dwell: np.ndarray = np.zeros(len(self.phases))
        self._prev = self.mean[0].copy()    # AR(1) memory
        # Start idle so the first emission is a quiet machine warming up.
        self._begin_idle()

    # -- cycle / idle scheduling ------------------------------------------
    def _begin_cycle(self, dt: float) -> None:
        self.active = True
        dur = float(self.rng.normal(self._cycle["duration_s_mean"], self._cycle["duration_s_std"]))
        dur = float(np.clip(dur, self._cycle["duration_s_min"], self._cycle["duration_s_max"]))
        # Allocate cycle time to phases proportional to their occupancy share,
        # with at least one tick each.
        self._phase_dwell = np.maximum(dt, self.phase_share * dur)
        self._phase_i = 0
        self._phase_remaining = self._phase_dwell[0]
        self._prev = self.mean[0].copy()

    def _begin_idle(self) -> None:
        self.active = False
        gap = float(self.rng.lognormal(self._idle["lognorm_mu"], self._idle["lognorm_sigma"]))
        self._mode_remaining = float(np.clip(gap, self._idle["gap_s_min"], self._idle["gap_s_max"]))

    def set_forced_mode(self, mode: str | None) -> None:
        """Operator override: pin the engine to a continuous machining cycle
        (``"ACTIVE"``), a quiet pause (``"IDLE"``), or ``None`` to resume the
        natural cycle/idle scheduling."""
        self.forced_mode = mode

    def step(self, dt: float) -> None:
        if self.forced_mode == "IDLE":
            self.active = False
            return
        if self.forced_mode == "ACTIVE":
            if not self.active:
                self._begin_cycle(dt)
                return
            self._phase_remaining -= dt
            while self._phase_remaining <= 0.0:
                self._phase_i += 1
                if self._phase_i >= len(self.phases):
                    # Loop straight into a fresh cycle instead of going idle.
                    self._begin_cycle(dt)
                    return
                self._phase_remaining += self._phase_dwell[self._phase_i]
            return
        if not self.active:
            self._mode_remaining -= dt
            if self._mode_remaining <= 0.0:
                self._begin_cycle(dt)
            return
        self._phase_remaining -= dt
        while self.active and self._phase_remaining <= 0.0:
            self._phase_i += 1
            if self._phase_i >= len(self.phases):
                self._begin_idle()
                break
            self._phase_remaining += self._phase_dwell[self._phase_i]

    # -- sampling ----------------------------------------------------------
    def sample(self) -> dict[str, float]:
        if not self.active:
            vals = self.rng.normal(0.0, self.idle_std)
            return {s: round(float(v), 4) for s, v in zip(self.sensors, vals)}
        i = self._phase_i
        mean, std = self.mean[i], self.std[i]
        innov = self.rng.normal(0.0, 1.0, size=len(self.sensors))
        nxt = mean + self.ar1 * (self._prev - mean) + np.sqrt(1.0 - self.ar1 ** 2) * std * innov
        nxt = np.clip(nxt, self.lo[i], self.hi[i])
        self._prev = nxt
        return {s: round(float(v), 4) for s, v in zip(self.sensors, nxt)}


def generate_frame(profile: dict, n_seconds: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Generate ``n_seconds`` of 1 Hz telemetry for offline training.

    Returns ``(values[T, n_sensors], active_mask[T])``. Callers typically train
    on the active samples only (idle is near-zero by construction).
    """
    eng = CncEngine(profile, rng=np.random.default_rng(seed))
    n = len(profile["sensors"])
    vals = np.empty((n_seconds, n), dtype=np.float32)
    mask = np.empty(n_seconds, dtype=bool)
    for t in range(n_seconds):
        eng.step(1.0)
        s = eng.sample()
        vals[t] = [s[k] for k in profile["sensors"]]
        mask[t] = eng.active
    return vals, mask
