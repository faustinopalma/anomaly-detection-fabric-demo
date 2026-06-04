"""Regime model — the discrete ``fase`` process that governs on/off behaviour.

We model ``fase`` as an empirical first-order Markov chain combined with an
explicit dwell-time (run-length) distribution per state. This semi-Markov
formulation reproduces both *which* state follows which (transition structure)
and *how long* the machine stays in each state (cycle/pause durations) far
better than a plain Markov chain, while remaining trivial to fit and fast to
sample — ideal as the conditioning signal for the diffusion model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class RegimeMarkov:
    n_states: int
    smoothing: float = 1.0
    min_dwell: int = 1
    # learned
    trans: list[list[float]] = field(default_factory=list)  # state->state (no self)
    dwell: dict[int, list[float]] = field(default_factory=dict)  # state -> run lengths
    start_probs: list[float] = field(default_factory=list)

    def fit(self, reg: np.ndarray) -> "RegimeMarkov":
        reg = np.asarray(reg, dtype=int)
        n = self.n_states
        # Dwell times per state from constant runs.
        change = np.flatnonzero(np.diff(reg) != 0)
        bounds = np.concatenate(([-1], change, [len(reg) - 1]))
        run_len = np.diff(bounds)
        run_state = reg[bounds[1:]]
        dwell: dict[int, list[float]] = {s: [] for s in range(n)}
        for s, ln in zip(run_state, run_len):
            if 0 <= s < n:
                dwell[int(s)].append(int(max(self.min_dwell, ln)))
        self.dwell = {k: v for k, v in dwell.items()}
        # State-to-state transitions between *runs* (exclude self loops; dwell
        # already captures self-persistence).
        trans = np.full((n, n), self.smoothing)
        np.fill_diagonal(trans, 0.0)
        for a, b in zip(run_state[:-1], run_state[1:]):
            if 0 <= a < n and 0 <= b < n and a != b:
                trans[a, b] += 1.0
        rs = trans.sum(axis=1, keepdims=True)
        rs[rs == 0] = 1.0
        self.trans = (trans / rs).tolist()
        # Start distribution from observed state occupancy.
        occ = np.bincount(reg[(reg >= 0) & (reg < n)], minlength=n).astype(float)
        occ = occ + self.smoothing
        self.start_probs = (occ / occ.sum()).tolist()
        return self

    def sample(self, length: int, seed: int | None = None) -> np.ndarray:
        """Sample a regime sequence of ``length`` grid steps."""
        rng = np.random.default_rng(seed)
        trans = np.asarray(self.trans)
        out = np.empty(length, dtype=int)
        state = int(rng.choice(self.n_states, p=np.asarray(self.start_probs)))
        i = 0
        while i < length:
            runs = self.dwell.get(state) or [self.min_dwell]
            dwell = int(rng.choice(runs))
            end = min(length, i + dwell)
            out[i:end] = state
            i = end
            probs = trans[state]
            if probs.sum() <= 0:
                state = int(rng.choice(self.n_states, p=np.asarray(self.start_probs)))
            else:
                state = int(rng.choice(self.n_states, p=probs / probs.sum()))
        return out

    # ---- persistence -----------------------------------------------------
    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "n_states": self.n_states,
                    "smoothing": self.smoothing,
                    "min_dwell": self.min_dwell,
                    "trans": self.trans,
                    "dwell": {str(k): v for k, v in self.dwell.items()},
                    "start_probs": self.start_probs,
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "RegimeMarkov":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        m = cls(n_states=d["n_states"], smoothing=d["smoothing"], min_dwell=d["min_dwell"])
        m.trans = d["trans"]
        m.dwell = {int(k): v for k, v in d["dwell"].items()}
        m.start_probs = d["start_probs"]
        return m
