"""Pipeline: fit the hybrid generator and compose synthetic telemetry.

The generator is a *composition* of three independently-fitted parts:

1. ``RegimeMarkov``        — samples the discrete ``fase`` trajectory.
2. ``ConditionalDiffusion`` — samples signal windows conditioned on the regime.
3. ``TimingModel``          — assigns irregular sub-second timestamps.

``fit`` trains everything and persists artifacts under ``cfg.out_path``;
``generate`` reloads (or reuses) them and emits a long-format DataFrame in the
exact real export schema (``ts, signal_name, value, measure_id``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config, SIGNAL_TO_MEASURE_ID
from .data import Dataset, build_dataset, time_split_windows
from .features import SignalScaler
from .models import ConditionalDiffusion, DiffusionConfig, RegimeMarkov, TimingModel


@dataclass
class SynthBundle:
    """A fully-fitted generator plus its scaler and config."""

    cfg: Config
    scaler: SignalScaler
    regime: RegimeMarkov
    timing: TimingModel
    diffusion: ConditionalDiffusion

    # ---- persistence -----------------------------------------------------
    def save(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        self.scaler.to_json(out / "scaler.json")
        self.regime.to_json(out / "regime.json")
        self.timing.to_json(out / "timing.json")
        self.diffusion.save(str(out / "diffusion.pt"))

    @classmethod
    def load(cls, cfg: Config, out: Path, device: str = "cpu") -> "SynthBundle":
        return cls(
            cfg=cfg,
            scaler=SignalScaler.from_json(out / "scaler.json"),
            regime=RegimeMarkov.from_json(out / "regime.json"),
            timing=TimingModel.from_json(out / "timing.json"),
            diffusion=ConditionalDiffusion.load(str(out / "diffusion.pt"), device=device),
        )


def _diffusion_cfg(cfg: Config) -> DiffusionConfig:
    """Bridge the project DiffusionConfig to the model's (adds window length)."""
    d = cfg.diffusion
    return DiffusionConfig(
        channels=d.channels,
        length=cfg.data.window,
        base_width=d.base_width,
        n_res_blocks=d.n_res_blocks,
        regime_embed_dim=d.regime_embed_dim,
        n_regimes=d.n_regimes,
        timesteps=d.timesteps,
        beta_start=d.beta_start,
        beta_end=d.beta_end,
        lr=d.lr,
        batch_size=d.batch_size,
        epochs=d.epochs,
        physics_lambda=d.physics_lambda,
        seed=d.seed,
    )


def fit(cfg: Config, ds: Dataset | None = None, *, save: bool = True) -> SynthBundle:
    """Fit scaler + the three sub-models on the dataset windows.

    Returns a :class:`SynthBundle`. Set ``save=False`` to skip writing artifacts
    (used inside the cloud entrypoint which writes to ``outputs/`` itself).
    """
    if ds is None:
        ds = build_dataset(cfg)
    if ds.n_windows == 0:
        raise ValueError("No training windows produced — check data subset/paths.")

    w_tr, r_tr, _w_va, _r_va, _w_te, _r_te = time_split_windows(ds, cfg)

    # 1) Scaler fit on flattened training windows [N*L, C].
    flat = w_tr.reshape(-1, w_tr.shape[2])
    scaler = SignalScaler(names=list(cfg.data.signals)).fit(flat)

    # 2) Regime + timing models (fit on the full grid / event stream).
    regime = RegimeMarkov(
        n_states=cfg.diffusion.n_regimes,
        smoothing=cfg.regime.smoothing,
        min_dwell=cfg.regime.min_dwell,
    ).fit(ds.grid[cfg.data.regime_col].to_numpy())
    timing = TimingModel(n_bins=cfg.timing.n_bins, max_gap_s=cfg.timing.max_gap_s).fit(
        ds.gaps_s, ds.gaps_regime
    )

    # 3) Diffusion on normalized windows.
    w_norm = np.stack([scaler.transform(win) for win in w_tr])
    diff = ConditionalDiffusion(_diffusion_cfg(cfg), device=cfg.device)
    t0 = time.time()
    diff.fit(w_norm, r_tr)
    print(f"[pipeline] diffusion trained in {time.time() - t0:.1f}s on {len(w_tr)} windows")

    bundle = SynthBundle(cfg=cfg, scaler=scaler, regime=regime, timing=timing, diffusion=diff)
    if save:
        bundle.save(cfg.out_path)
        print(f"[pipeline] artifacts saved to {cfg.out_path}")
    return bundle


def generate(
    bundle: SynthBundle,
    n_steps: int,
    *,
    start_ts: pd.Timestamp | None = None,
    seed: int | None = None,
    long_format: bool = True,
) -> pd.DataFrame:
    """Generate ``n_steps`` grid samples of synthetic telemetry.

    Returns a long-format DataFrame (``ts, signal_name, value, measure_id``) by
    default, or a wide frame indexed by ``ts`` when ``long_format=False``.
    """
    cfg = bundle.cfg
    rng = np.random.default_rng(seed)
    win = cfg.data.window

    # 1) Regime trajectory over the requested horizon.
    regime_seq = bundle.regime.sample(n_steps, seed=seed)

    # 2) Diffusion: one conditioned window per non-overlapping block, stitched.
    n_blocks = int(np.ceil(n_steps / win))
    block_regime = np.array(
        [int(regime_seq[min(b * win, n_steps - 1)]) for b in range(n_blocks)], dtype=int
    )
    norm_windows = bundle.diffusion.sample(block_regime, seed=seed)  # [B, L, C]
    sig_norm = norm_windows.reshape(-1, norm_windows.shape[2])[:n_steps]
    signals = bundle.scaler.inverse_transform(sig_norm)  # [n_steps, C] real units

    # 3) Timestamps: irregular cadence conditioned on the regime per step.
    if start_ts is None:
        start_ts = pd.Timestamp.utcnow()
    start_epoch = start_ts.timestamp()
    ts_epoch = bundle.timing.sample_timestamps(regime_seq, start_epoch, seed=seed)
    ts = pd.to_datetime(ts_epoch, unit="s", utc=True)

    names = list(cfg.data.signals)
    wide = pd.DataFrame(signals, columns=names, index=pd.DatetimeIndex(ts, name="ts"))
    wide[cfg.data.regime_col] = regime_seq

    if not long_format:
        return wide

    # Long format matching the real export schema.
    frames = []
    for name in names + [cfg.data.regime_col]:
        col = wide[name]
        frames.append(
            pd.DataFrame(
                {
                    "ts": wide.index,
                    "signal_name": name,
                    "value": col.to_numpy(),
                    "measure_id": SIGNAL_TO_MEASURE_ID.get(name, ""),
                }
            )
        )
    long = pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)
    return long
