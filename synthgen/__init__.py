"""synthgen — state-of-the-art synthetic generator for CNC spindle telemetry.

The package is intentionally split into small, independently testable modules so
that the *same* code runs in fast local loops (CPU, subset of data, few epochs)
and in a full Azure ML GPU training job (no code fork, only config changes):

- ``synthgen.config``   : typed configuration (local vs cloud) loaded from YAML.
- ``synthgen.data``     : load real parquet, segment work-cycles, build windows.
- ``synthgen.features`` : robust per-signal normalization (heavy-tail safe).
- ``synthgen.metrics``  : fidelity metrics (the "judge": marginals, correlation,
                          spectra, regime durations, discriminative/predictive).
- ``synthgen.models``   : regime (HMM/Markov), conditional diffusion, timing.
- ``synthgen.pipeline`` : compose the three components into ``generate()``.
- ``synthgen.aml``      : submit / poll / download helpers for Azure ML.

Design rule: notebooks orchestrate, the package does the work.
"""
from __future__ import annotations

__all__ = [
    "config",
    "data",
    "features",
    "metrics",
    "models",
    "pipeline",
    "aml",
]

__version__ = "0.1.0"
