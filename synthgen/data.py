"""Data loading, resampling, work-cycle segmentation and window construction.

The real telemetry is an irregularly-sampled (sub-second) multivariate stream of
one CNC spindle. For the *signal* model we work on a regular grid (windows of
fixed length); for the *timing* model we keep the original inter-arrival gaps;
for the *regime* model we use the discrete ``fase`` sequence. This module
produces all three views from the same source parquet.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config, REGIME_COL


@dataclass
class Dataset:
    """All views needed by the three generator components."""

    # Original, event-level (irregular) frame: index = ts (tz-aware), columns =
    # signals + regime. NaNs preserved where a signal was not sampled.
    events: pd.DataFrame
    # Grid-resampled frame (regular cadence), forward/interp filled.
    grid: pd.DataFrame
    # Windows for the diffusion model: [N, window, C] (normalized later).
    windows: np.ndarray
    # Regime id per window-start (int), shape [N].
    window_regime: np.ndarray
    # Original inter-arrival gaps in seconds, with regime id, for timing model.
    gaps_s: np.ndarray
    gaps_regime: np.ndarray
    signals: list[str]
    regime_col: str

    @property
    def n_windows(self) -> int:
        return int(self.windows.shape[0])


def load_wide(cfg: Config) -> pd.DataFrame:
    """Load the pivoted wide parquet, indexed by tz-aware ``ts``."""
    path = cfg.resolve(cfg.data.wide_path)
    df = pd.read_parquet(path)
    if df.index.name != "ts":
        # Some exports keep ts as a column.
        if "ts" in df.columns:
            df = df.set_index("ts")
    df = df.sort_index()
    # Keep only the signals we model + the regime column.
    keep = [c for c in cfg.data.signals if c in df.columns] + [cfg.data.regime_col]
    df = df[keep]
    return df


def _apply_subset(df: pd.DataFrame, subset_days: float | None) -> pd.DataFrame:
    if subset_days is None:
        return df
    end = df.index.max()
    start = end - pd.Timedelta(days=float(subset_days))
    return df.loc[df.index >= start]


def _resample_grid(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Resample the irregular events onto a regular grid.

    Signals are linearly interpolated over short gaps and the discrete regime is
    forward-filled. Long idle gaps are left as-is after a bounded interpolation.
    """
    rule = f"{cfg.data.resample_ms}ms"
    sig = cfg.data.signals
    # Mean within each bin keeps bursts representative without exploding size.
    g = df.resample(rule).mean(numeric_only=True)
    # Bounded interpolation for signals (don't bridge long machine-off gaps).
    limit = max(1, int(round(2_000 / cfg.data.resample_ms)))  # ~2 s
    g[sig] = g[sig].interpolate(method="linear", limit=limit, limit_area="inside")
    # Regime: forward fill (state persists), then back/zero fill the head.
    g[cfg.data.regime_col] = (
        g[cfg.data.regime_col].ffill().bfill().round().astype("int64")
    )
    return g


def _make_windows(grid: pd.DataFrame, cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    """Slice the grid into overlapping windows with a clean (NaN-free) signal."""
    sig = cfg.data.signals
    w, s = cfg.data.window, cfg.data.stride
    vals = grid[sig].to_numpy(dtype=np.float64)
    reg = grid[cfg.data.regime_col].to_numpy()
    n = vals.shape[0]
    windows: list[np.ndarray] = []
    regimes: list[int] = []
    for start in range(0, max(0, n - w + 1), s):
        chunk = vals[start : start + w]
        if not np.isfinite(chunk).all():
            continue  # skip windows that span a machine-off gap
        windows.append(chunk)
        regimes.append(int(reg[start]))
    if not windows:
        return np.empty((0, w, len(sig))), np.empty((0,), dtype=int)
    return np.stack(windows), np.asarray(regimes, dtype=int)


def _inter_arrival(df: pd.DataFrame, cfg: Config) -> tuple[np.ndarray, np.ndarray]:
    """Inter-arrival gaps (s) from the original event timestamps + regime id."""
    ts = df.index.view("int64") / 1e9  # epoch seconds
    gaps = np.diff(ts)
    reg = df[cfg.data.regime_col].to_numpy()[1:]
    cap = cfg.timing.max_gap_s
    mask = (gaps > 0) & (gaps <= cap)
    return gaps[mask], reg[mask].astype(int)


def build_dataset(cfg: Config) -> Dataset:
    """End-to-end build of the three data views from the wide parquet."""
    df = load_wide(cfg)
    df = _apply_subset(df, cfg.subset_days)
    grid = _resample_grid(df, cfg)
    windows, window_regime = _make_windows(grid, cfg)
    gaps_s, gaps_regime = _inter_arrival(df, cfg)
    return Dataset(
        events=df,
        grid=grid,
        windows=windows,
        window_regime=window_regime,
        gaps_s=gaps_s,
        gaps_regime=gaps_regime,
        signals=list(cfg.data.signals),
        regime_col=cfg.data.regime_col,
    )


def time_split_windows(
    ds: Dataset, cfg: Config
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split windows into train/val/test by *time order* (no shuffling)."""
    n = ds.n_windows
    i_tr = int(n * cfg.data.train_frac)
    i_va = int(n * (cfg.data.train_frac + cfg.data.val_frac))
    w, r = ds.windows, ds.window_regime
    return (
        w[:i_tr], r[:i_tr],
        w[i_tr:i_va], r[i_tr:i_va],
        w[i_va:], r[i_va:],
    )
