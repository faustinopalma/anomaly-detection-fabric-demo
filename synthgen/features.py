"""Robust, heavy-tail-safe per-signal normalization.

The real spindle signals have heavy tails, negative values (regenerative /
measurement), and very different scales (torque is in the thousands, power in
the tens). A plain z-score lets the torque channel dominate and makes the
diffusion model waste capacity on the tails. We use a *signed log-modulus*
transform — ``sign(x) * log1p(|x| / s)`` — which is symmetric, defined for the
negative range, and compresses tails, followed by a standardization. The fitted
parameters are persisted as JSON so the exact inverse can be applied locally and
in the cloud without retraining.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class SignalScaler:
    """Per-channel signed-log-modulus + standardization.

    Parameters are learned with :meth:`fit` and stored channel-wise so that the
    transform is fully reversible via :meth:`inverse_transform`.
    """

    names: list[str]
    # learned, one entry per channel
    log_scale: list[float] = field(default_factory=list)  # ``s`` in log1p(|x|/s)
    center: list[float] = field(default_factory=list)
    spread: list[float] = field(default_factory=list)

    # ---- fit / transform -------------------------------------------------
    def fit(self, x: np.ndarray) -> "SignalScaler":
        """Fit on an array of shape ``[N, C]`` (NaNs ignored)."""
        x = np.asarray(x, dtype=np.float64)
        self.log_scale, self.center, self.spread = [], [], []
        for c in range(x.shape[1]):
            col = x[:, c]
            col = col[np.isfinite(col)]
            # Robust scale: median absolute value (fallback to 1).
            s = float(np.median(np.abs(col))) or 1.0
            t = np.sign(col) * np.log1p(np.abs(col) / s)
            mu = float(np.mean(t))
            sd = float(np.std(t)) or 1.0
            self.log_scale.append(s)
            self.center.append(mu)
            self.spread.append(sd)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        out = np.empty_like(x)
        for c in range(x.shape[1]):
            s, mu, sd = self.log_scale[c], self.center[c], self.spread[c]
            t = np.sign(x[:, c]) * np.log1p(np.abs(x[:, c]) / s)
            out[:, c] = (t - mu) / sd
        return out

    def inverse_transform(self, z: np.ndarray) -> np.ndarray:
        z = np.asarray(z, dtype=np.float64)
        out = np.empty_like(z)
        for c in range(z.shape[1]):
            s, mu, sd = self.log_scale[c], self.center[c], self.spread[c]
            t = z[:, c] * sd + mu
            out[:, c] = np.sign(t) * (np.expm1(np.abs(t)) * s)
        return out

    # ---- persistence -----------------------------------------------------
    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "names": self.names,
                    "log_scale": self.log_scale,
                    "center": self.center,
                    "spread": self.spread,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "SignalScaler":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            names=d["names"],
            log_scale=d["log_scale"],
            center=d["center"],
            spread=d["spread"],
        )
