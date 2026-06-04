"""Timing model — reproduces the irregular, sub-second sampling cadence.

The real stream is sampled at irregular ~0.2-0.3 s intervals with bursts and
machine-off gaps. We learn a per-regime histogram of inter-arrival gaps and
sample from it (inverse-CDF), so the synthetic timestamps inherit the cadence
*conditioned on the machine state*. This is a lightweight, robust alternative to
a full Hawkes process and is sufficient for telemetry replay fidelity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class TimingModel:
    n_bins: int = 60
    max_gap_s: float = 5.0
    # learned: per-regime CDF over gap bins; key -1 is the global fallback.
    bin_edges: list[float] = field(default_factory=list)
    cdfs: dict[int, list[float]] = field(default_factory=dict)

    def fit(self, gaps_s: np.ndarray, regime: np.ndarray) -> "TimingModel":
        gaps_s = np.asarray(gaps_s, dtype=float)
        regime = np.asarray(regime, dtype=int)
        edges = np.linspace(0.0, self.max_gap_s, self.n_bins + 1)
        self.bin_edges = edges.tolist()

        def make_cdf(g: np.ndarray) -> list[float]:
            if g.size == 0:
                return (np.ones(self.n_bins) / self.n_bins).cumsum().tolist()
            hist, _ = np.histogram(np.clip(g, 0, self.max_gap_s), bins=edges)
            hist = hist.astype(float) + 1e-6
            return (hist / hist.sum()).cumsum().tolist()

        self.cdfs = {-1: make_cdf(gaps_s)}
        for s in np.unique(regime):
            self.cdfs[int(s)] = make_cdf(gaps_s[regime == s])
        return self

    def sample_gap(self, state: int, rng: np.random.Generator) -> float:
        cdf = np.asarray(self.cdfs.get(state, self.cdfs[-1]))
        u = rng.random()
        idx = int(np.searchsorted(cdf, u))
        idx = min(idx, self.n_bins - 1)
        lo, hi = self.bin_edges[idx], self.bin_edges[idx + 1]
        return float(rng.uniform(lo, hi))

    def sample_timestamps(
        self, regime_per_step: np.ndarray, start_epoch_s: float, seed: int | None = None
    ) -> np.ndarray:
        """Generate increasing epoch-second timestamps, one per regime step."""
        rng = np.random.default_rng(seed)
        ts = np.empty(len(regime_per_step), dtype=float)
        t = float(start_epoch_s)
        for i, s in enumerate(regime_per_step):
            ts[i] = t
            t += self.sample_gap(int(s), rng)
        return ts

    # ---- persistence -----------------------------------------------------
    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "n_bins": self.n_bins,
                    "max_gap_s": self.max_gap_s,
                    "bin_edges": self.bin_edges,
                    "cdfs": {str(k): v for k, v in self.cdfs.items()},
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "TimingModel":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        m = cls(n_bins=d["n_bins"], max_gap_s=d["max_gap_s"])
        m.bin_edges = d["bin_edges"]
        m.cdfs = {int(k): v for k, v in d["cdfs"].items()}
        return m
