"""Verify that the M-002 synthgen replay trace is aligned with the real sample.

The simulator drives M-002 by *replaying* ``simulator-cloud/src/synth_trace_M-002.json``
(produced by ``tools/build_synth_trace.py`` from the synthgen hybrid model). If
the training job that fitted that generator failed or was under-trained, the
shipped trace could be out of distribution versus the real customer telemetry.

This script answers, with no Azure access, the question *"are the values that
M-002 emits today aligned with the real sample?"* by comparing marginal stats,
quantiles, duty cycle and cross-signal correlations of the shipped trace against
the real wide parquet. It prints a per-signal table and a PASS/WARN/FAIL verdict
based on relative error tolerances on the active-sample distribution.

Usage
-----
    .venv/Scripts/python.exe tools/_verify_synth_trace.py
    .venv/Scripts/python.exe tools/_verify_synth_trace.py --full   # use the full npz trace too
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
SIGNALS = ("mandrino_load", "mandrino_power", "mandrino_torque")
ACTIVE_THRESHOLD = 2.0  # configs/synthgen.yaml data.active_load_threshold

TRACE_JSON = REPO / "simulator-cloud" / "src" / "synth_trace_M-002.json"
FULL_NPZ = REPO / "_local" / "synthgen" / "synth_trace_full.npz"
REAL_PARQUET = REPO / "_data_local" / "cnc_real_wide.parquet"

# Relative-error tolerances on the active-sample distribution. Synthetic data is
# never a bit-exact match; these bound how far the generator may drift before we
# flag it. mean/std are the headline moments; range is informational.
TOL_WARN = 0.15
TOL_FAIL = 0.35


def _active_mask(load: np.ndarray) -> np.ndarray:
    return np.abs(load) > ACTIVE_THRESHOLD


def _load_trace_json(path: Path) -> tuple[np.ndarray, np.ndarray]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    values = np.asarray(trace["values"], dtype=np.float64)
    active = np.asarray(trace.get("active", np.ones(len(values))), dtype=bool)
    sensors = list(trace["sensors"])
    if sensors != list(SIGNALS):
        raise SystemExit(f"trace sensor order {sensors} != expected {list(SIGNALS)}")
    print(f"[trace] {path.name}: {len(values):,} steps, "
          f"duty(meta)={trace.get('duty_cycle')}, kind={trace.get('kind')}")
    return values, active


def _load_full_npz(path: Path) -> tuple[np.ndarray, np.ndarray]:
    z = np.load(path, allow_pickle=True)
    values = np.asarray(z["values"], dtype=np.float64)
    active = np.asarray(z["active"], dtype=bool)
    print(f"[trace] {path.name}: {len(values):,} steps (full training trace)")
    return values, active


def _real_active(parquet: Path) -> np.ndarray:
    df = pd.read_parquet(parquet)
    missing = [s for s in SIGNALS if s not in df.columns]
    if missing:
        raise SystemExit(f"real parquet missing signals {missing}; has {list(df.columns)}")
    vals = df[list(SIGNALS)].to_numpy(dtype=np.float64)
    nan_rows = np.isnan(vals).any(axis=1)
    load = vals[:, SIGNALS.index("mandrino_load")]
    mask = _active_mask(load) & ~nan_rows
    print(f"[real ] {parquet.name}: {len(df):,} rows, "
          f"{int(nan_rows.sum()):,} rows dropped for NaN, "
          f"active={mask.mean():.1%} (|load|>{ACTIVE_THRESHOLD}, NaN-free)")
    return vals[mask]


def _rel(a: float, b: float) -> float:
    """Relative error of a vs reference b (symmetric-ish, guarded)."""
    denom = max(abs(b), 1e-9)
    return abs(a - b) / denom


def _verdict(rel: float) -> str:
    if rel <= TOL_WARN:
        return "PASS"
    if rel <= TOL_FAIL:
        return "WARN"
    return "FAIL"


def _compare(real_active: np.ndarray, synth: np.ndarray, synth_active_mask: np.ndarray,
             label: str) -> int:
    synth_active = synth[synth_active_mask]
    print(f"\n=== {label}: active-sample marginals (real vs synth) ===")
    print(f"  duty: real n={len(real_active):,}  synth active n={len(synth_active):,} "
          f"({synth_active_mask.mean():.1%} of trace)")
    header = (f"  {'signal':<16} {'real mean':>10} {'synth mean':>11} {'Δmean':>7} "
              f"{'real std':>10} {'synth std':>10} {'Δstd':>7}  verdict")
    print(header)
    worst = "PASS"
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    for i, s in enumerate(SIGNALS):
        rv = real_active[:, i]
        sv = synth_active[:, i]
        rmean, smean = rv.mean(), sv.mean()
        rstd, sstd = rv.std(), sv.std()
        dmean = _rel(smean, rmean)
        dstd = _rel(sstd, rstd)
        v = _verdict(max(dmean, dstd))
        if rank[v] > rank[worst]:
            worst = v
        print(f"  {s:<16} {rmean:>10.2f} {smean:>11.2f} {dmean:>6.1%} "
              f"{rstd:>10.2f} {sstd:>10.2f} {dstd:>6.1%}  {v}")

    # Quantiles (p05/p50/p95) — distribution shape, not just moments.
    print(f"\n  {'signal':<16} {'q':>4} {'real':>10} {'synth':>10} {'Δ':>7}")
    for i, s in enumerate(SIGNALS):
        rv = real_active[:, i]
        sv = synth_active[:, i]
        for q in (0.05, 0.50, 0.95):
            rq = float(np.quantile(rv, q))
            sq = float(np.quantile(sv, q))
            print(f"  {s if q == 0.05 else '':<16} {int(q*100):>3}% "
                  f"{rq:>10.2f} {sq:>10.2f} {_rel(sq, rq):>6.1%}")

    # Cross-signal correlation structure (physics coupling load↔power↔torque).
    rc = np.corrcoef(real_active.T)
    sc = np.corrcoef(synth_active.T)
    print("\n  cross-signal correlation (real -> synth):")
    pairs = [(0, 1, "load~power"), (0, 2, "load~torque"), (1, 2, "power~torque")]
    for a, b, name in pairs:
        print(f"    {name:<14} {rc[a, b]:>6.3f} -> {sc[a, b]:>6.3f} "
              f"(Δ {abs(rc[a, b]-sc[a, b]):.3f})")

    print(f"\n  >>> {label} verdict: {worst}")
    return rank[worst]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="Also compare the full training npz trace.")
    args = ap.parse_args(argv)

    if not REAL_PARQUET.exists():
        raise SystemExit(f"real data not found: {REAL_PARQUET}")
    if not TRACE_JSON.exists():
        raise SystemExit(f"shipped trace not found: {TRACE_JSON}")

    real_active = _real_active(REAL_PARQUET)

    values, active = _load_trace_json(TRACE_JSON)
    worst = _compare(real_active, values, active, "SHIPPED trace (synth_trace_M-002.json)")

    if args.full and FULL_NPZ.exists():
        fvals, factive = _load_full_npz(FULL_NPZ)
        worst = max(worst, _compare(real_active, fvals, factive, "FULL training trace (npz)"))

    label = {0: "PASS", 1: "WARN", 2: "FAIL"}[worst]
    print("\n" + "=" * 70)
    print(f"OVERALL VERDICT: {label}")
    if label == "PASS":
        print("The M-002 trace is statistically aligned with the real sample.")
    elif label == "WARN":
        print("The M-002 trace is broadly aligned but some moments drift "
              f"more than {TOL_WARN:.0%}. Usable for the demo; consider a "
              "longer training run for tighter fidelity.")
    else:
        print("The M-002 trace is OUT OF DISTRIBUTION vs the real sample "
              f"(>{TOL_FAIL:.0%} drift). Rebuild it with a successfully "
              "trained generator (tools/build_synth_trace.py / notebook 08).")
    print("=" * 70)
    return 0 if worst < 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
