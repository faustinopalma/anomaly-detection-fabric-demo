"""Typed configuration for the synthetic-data generator.

A single ``Config`` object drives both the fast *local* methodology loops and
the full *cloud* (Azure ML) training run. The only thing that changes between
the two is a handful of fields (subset size, epochs, device); the model code is
identical. Config is loaded from ``configs/synthgen.yaml`` and can be overridden
per-mode or programmatically.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repository root (…/anomalydetection). This file lives at synthgen/config.py.
REPO_ROOT = Path(__file__).resolve().parents[1]

# The four real signals, in a fixed channel order used everywhere downstream.
SIGNALS: tuple[str, ...] = ("mandrino_load", "mandrino_power", "mandrino_torque")
REGIME_COL: str = "fase"

# Mapping back to the real export schema (long format) for generated output.
SIGNAL_TO_MEASURE_ID: dict[str, str] = {
    "mandrino_load": "9",
    "mandrino_power": "11",
    "mandrino_torque": "10",
    "fase": "17",
}


@dataclass
class DataConfig:
    wide_path: str = "_data_local/cnc_real_wide.parquet"
    raw_dir: str = "_data_local/parquet_files_raw"
    signals: list[str] = field(default_factory=lambda: list(SIGNALS))
    regime_col: str = REGIME_COL
    # A window of consecutive (grid-aligned) samples is one training example.
    window: int = 128
    stride: int = 64
    # Resampling grid used for window construction and several metrics.
    resample_ms: int = 200  # ~ the native sub-second cadence (0.2 s)
    # A sample is "active" (machine cutting) when |load| exceeds this.
    active_load_threshold: float = 2.0
    # Time-ordered split fractions (no leakage).
    train_frac: float = 0.7
    val_frac: float = 0.15  # test = remainder


@dataclass
class DiffusionConfig:
    channels: int = len(SIGNALS)
    base_width: int = 64
    n_res_blocks: int = 2
    regime_embed_dim: int = 16
    n_regimes: int = 32  # fase is 0..24; pad to be safe
    timesteps: int = 200  # diffusion steps (small for local; raise on cloud)
    beta_start: float = 1e-4
    beta_end: float = 0.02
    lr: float = 2e-4
    batch_size: int = 64
    epochs: int = 2  # LOCAL smoke default; cloud overrides to e.g. 200
    physics_lambda: float = 0.1  # weight of load/power/torque coherence penalty
    seed: int = 7


@dataclass
class RegimeConfig:
    # Empirical Markov chain on the discrete ``fase`` plus dwell-time sampling.
    smoothing: float = 1.0  # Laplace smoothing on the transition matrix
    min_dwell: int = 1


@dataclass
class TimingConfig:
    # Per-regime inter-arrival (gap) sampling to reproduce irregular cadence.
    max_gap_s: float = 5.0  # cap pathological gaps when learning the histogram
    n_bins: int = 60


@dataclass
class AmlConfig:
    subscription: str = "7ecf802f-04ac-4e81-8703-c3d39074f823"
    resource_group: str = "rg-anomaly-ml-westeurope"
    workspace: str = "anomalyml-mlw"
    gpu_cluster: str = "gpu-t4-cluster"
    gpu_sku: str = "Standard_NC4as_T4_v3"  # 1x T4, within NCASv3_T4=16 quota
    experiment_name: str = "synthgen-diffusion"
    environment_image: str = (
        "mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu20.04:latest"
    )


@dataclass
class Config:
    mode: str = "local"  # "local" | "cloud"
    device: str = "cpu"  # "cpu" | "cuda"
    out_dir: str = "_local/synthgen"  # artifacts (scalers, checkpoints, reports)
    # When set, only use the last N days of real data (fast local loops).
    subset_days: float | None = 3.0
    data: DataConfig = field(default_factory=DataConfig)
    diffusion: DiffusionConfig = field(default_factory=DiffusionConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    aml: AmlConfig = field(default_factory=AmlConfig)

    # ---- convenience -----------------------------------------------------
    @property
    def out_path(self) -> Path:
        p = (REPO_ROOT / self.out_dir) if not Path(self.out_dir).is_absolute() else Path(self.out_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve(self, rel: str) -> Path:
        """Resolve a possibly-relative path against the repo root."""
        p = Path(rel)
        return p if p.is_absolute() else (REPO_ROOT / p)


def _apply_overrides(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _apply_overrides(out[k], v)
        else:
            out[k] = v
    return out


def _build(cfg_dict: dict[str, Any]) -> Config:
    sub = {
        "data": DataConfig(**cfg_dict.pop("data", {}) or {}),
        "diffusion": DiffusionConfig(**cfg_dict.pop("diffusion", {}) or {}),
        "regime": RegimeConfig(**cfg_dict.pop("regime", {}) or {}),
        "timing": TimingConfig(**cfg_dict.pop("timing", {}) or {}),
        "aml": AmlConfig(**cfg_dict.pop("aml", {}) or {}),
    }
    return Config(**cfg_dict, **sub)


def load_config(mode: str = "local", path: str | Path | None = None) -> Config:
    """Load configuration for the requested mode.

    The YAML may contain a top-level ``defaults`` block plus per-mode blocks
    ``local`` / ``cloud`` whose values override the defaults.
    """
    if path is None:
        path = REPO_ROOT / "configs" / "synthgen.yaml"
    path = Path(path)
    if not path.exists():
        # Fall back to dataclass defaults (still fully functional).
        return Config(mode=mode, device="cuda" if mode == "cloud" else "cpu")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = raw.get("defaults", {}) or {}
    over = raw.get(mode, {}) or {}
    merged = _apply_overrides(base, over)
    merged.setdefault("mode", mode)
    return _build(merged)


def to_dict(cfg: Config) -> dict[str, Any]:
    return dataclasses.asdict(cfg)
