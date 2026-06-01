"""Reorganize the raw CNC spindle data and derive an empirical machine profile.

The raw export under ``_data_local/parquet_files_raw/*.parquet`` is in long /
EAV form (one row per signal sample):

    [ts, work_date, signal_name, value, measure_id, export_timestamp]

It contains a real CNC machining engine-heads (spindle = "mandrino"). Four
signals are present:

    mandrino_load   spindle load     (measure_id 9)   ~3.3 Hz   unit: %  (drive load percent)
    mandrino_power  spindle power     (measure_id 11)  ~3.3 Hz   unit: kW
    mandrino_torque spindle torque    (measure_id 10)  ~3.3 Hz   unit: N*cm (drive torque register)
    fase            machining phase   (measure_id 17)  event     unit: -  (discrete cycle phase code)

This tool:

1. Pivots the three continuous spindle signals to a wide frame on their shared
   timestamp grid and forward-fills the discrete ``fase`` onto it.
2. Saves the reorganized wide frame to ``_data_local/cnc_real_wide.parquet``
   (kept local; the raw data is customer-confidential and git-ignored).
3. Derives an *aggregate* empirical machine profile - cycle duration, idle-gap
   distribution, per-phase time-share, per-phase signal mean/std/min/max and
   per-signal AR(1) autocorrelation - and writes it to
   ``data/cnc_profile_M-003.json``. Only de-identified aggregate parameters
   leave ``_data_local/``; this JSON is the single source of truth shared by
   the live simulator (``CNCMachine``) and the M-003 training generator.

Usage:
    .venv/Scripts/python.exe tools/cnc_build_profile.py
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
RAW_GLOB = str(REPO / "_data_local" / "parquet_files_raw" / "*.parquet")
WIDE_OUT = REPO / "_data_local" / "cnc_real_wide.parquet"
PROFILE_OUT = REPO / "data" / "cnc_profile_M-003.json"

CONT = ["mandrino_load", "mandrino_power", "mandrino_torque"]
UNITS = {
    "mandrino_load": "%",
    "mandrino_power": "kW",
    "mandrino_torque": "N*cm",
}
# Phases with negligible occupancy (<0.1% of active time) are dropped from the
# cycle model; they are transient artefacts of the forward-fill, not real
# machining phases.
MIN_PHASE_SHARE = 0.001
# A continuous-sample gap larger than this marks the boundary between two
# machining cycles (the real machine simply stops emitting between parts).
RUN_GAP_S = 5.0
# Target emission rate of the live simulator / training generator (Hz). The KQL
# pipeline bins at 1 s and the models use WINDOW=64, so we model M-003 at 1 Hz
# like M-001/M-002 (a 64-sample window ~ 1.5 machining cycles).
TARGET_HZ = 1.0


def load_wide() -> pd.DataFrame:
    files = sorted(glob.glob(RAW_GLOB))
    if not files:
        raise SystemExit(f"No raw parquet found under {RAW_GLOB}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.sort_values("ts").reset_index(drop=True)

    wide = df[df.signal_name.isin(CONT)].pivot_table(
        index="ts", columns="signal_name", values="value", aggfunc="mean")
    wide = wide[CONT]

    fase = df[df.signal_name == "fase"][["ts", "value"]].copy()
    fase = fase.rename(columns={"value": "fase"}).set_index("ts")["fase"]
    fase = fase[~fase.index.duplicated(keep="last")]
    wide["fase"] = fase.reindex(wide.index, method="ffill").fillna(0).round().astype(int)
    return wide


def native_hz(wide: pd.DataFrame) -> float:
    dt = wide.index.to_series().diff().dt.total_seconds()
    med = float(dt[dt <= RUN_GAP_S].median())
    return 1.0 / med


def segment_runs(wide: pd.DataFrame) -> pd.Series:
    dt = wide.index.to_series().diff().dt.total_seconds()
    return (dt > RUN_GAP_S).cumsum()


def resample_active_1hz(wide: pd.DataFrame, seg: pd.Series) -> pd.DataFrame:
    """Down-sample each active run to the target rate (1 Hz) by averaging.

    Per-phase signal statistics and the AR(1) coefficient must be measured at
    the rate the simulator/model actually emit (TARGET_HZ), not at the ~3.3 Hz
    native rate - otherwise the synthetic stream is noisier and less
    autocorrelated than real 1 Hz telemetry.
    """
    step = f"{int(round(1.0 / TARGET_HZ))}s"
    out = []
    for _, g in wide.groupby(seg):
        if (g.index[-1] - g.index[0]).total_seconds() < RUN_GAP_S:
            continue
        r = g[CONT + ["fase"]].resample(step).mean()
        r["fase"] = r["fase"].round().ffill()
        out.append(r.dropna(subset=CONT))
    return pd.concat(out)


def build_profile(wide: pd.DataFrame) -> dict:
    hz = native_hz(wide)
    seg = segment_runs(wide)
    dt = wide.index.to_series().diff().dt.total_seconds()
    # Statistics that define the per-sample signal regime are measured on the
    # 1 Hz-resampled active stream (the emission rate); wall-clock structure
    # (cycle duration, idle gaps) is measured on the native timeline.
    onehz = resample_active_1hz(wide, seg)

    # --- cycle (active run) duration -------------------------------------
    run_dur = wide.groupby(seg).apply(
        lambda g: (g.index[-1] - g.index[0]).total_seconds(), include_groups=False)
    run_dur = run_dur[run_dur > 1.0]
    cycle = {
        "duration_s_mean": float(run_dur.mean()),
        "duration_s_std": float(run_dur.std()),
        "duration_s_min": float(max(8.0, run_dur.quantile(0.02))),
        "duration_s_max": float(run_dur.quantile(0.98)),
    }

    # --- idle gap between cycles -----------------------------------------
    gaps = dt[dt > RUN_GAP_S]
    # Drop multi-hour break/weekend gaps for the live demo; keep the realistic
    # short part-to-part pauses and fit a log-normal to them.
    short = gaps[(gaps >= RUN_GAP_S) & (gaps <= 600)]
    logs = np.log(short.to_numpy())
    idle = {
        "gap_s_median": float(gaps.median()),
        "gap_s_p90": float(gaps.quantile(0.90)),
        "lognorm_mu": float(logs.mean()),
        "lognorm_sigma": float(logs.std()),
        "gap_s_min": 8.0,
        "gap_s_max": 180.0,
    }

    # --- per-phase occupancy + signal stats (measured at 1 Hz) ----------
    share = onehz["fase"].value_counts(normalize=True)
    phases = sorted(int(p) for p, s in share.items() if s >= MIN_PHASE_SHARE and p != 0)
    sub = share[phases]
    sub = sub / sub.sum()
    phase_time_share = {str(p): float(sub[p]) for p in phases}

    stats: dict[str, dict] = {}
    for p in phases:
        block = onehz[onehz["fase"] == p]
        stats[str(p)] = {
            s: {
                "mean": float(block[s].mean()),
                "std": float(block[s].std()),
                "min": float(block[s].min()),
                "max": float(block[s].max()),
            }
            for s in CONT
        }

    # --- AR(1) autocorrelation, measured directly at 1 Hz ----------------
    onehz_seg = (onehz.index.to_series().diff().dt.total_seconds() > RUN_GAP_S).cumsum()
    ar1: dict[str, float] = {}
    for s in CONT:
        acs = []
        for _, g in onehz.groupby(onehz_seg):
            v = g[s].to_numpy()
            if len(v) > 10:
                a = np.corrcoef(v[:-1], v[1:])[0, 1]
                if np.isfinite(a):
                    acs.append(a)
        ar1[s] = float(np.clip(np.median(acs), 0.0, 0.95))

    # --- idle-period signal level (machine not cutting) -------------------
    idle_signal = {
        s: {"mean": 0.0, "std": float(max(0.2, onehz[s].abs().std() * 0.02))}
        for s in CONT
    }

    span = (wide.index[-1] - wide.index[0]).total_seconds()
    active = float(dt[(dt <= RUN_GAP_S)].sum())

    return {
        "machine_id": "M-003",
        "kind": "cnc_spindle",
        "description": (
            "CNC spindle machining engine cylinder heads. Empirical profile "
            "derived from real customer telemetry (1.17M samples, 15 work "
            "days). Three measured spindle signals; the discrete machining "
            "phase 'fase' drives the per-phase signal regimes but is not "
            "itself scored."
        ),
        "source": "real_cnc_export",
        "native_sample_rate_hz": round(hz, 3),
        "sample_rate_hz": TARGET_HZ,
        "duty_cycle": round(active / span, 4),
        "sensors": CONT,
        "units": UNITS,
        "phases": phases,
        "phase_time_share": phase_time_share,
        "phase_signal_stats": stats,
        "ar1": ar1,
        "cycle": cycle,
        "idle": idle,
        "idle_signal": idle_signal,
    }


def main() -> int:
    wide = load_wide()
    WIDE_OUT.parent.mkdir(parents=True, exist_ok=True)
    wide.to_parquet(WIDE_OUT)
    print(f"[ok] reorganized wide frame -> {WIDE_OUT.relative_to(REPO)}  shape={wide.shape}")

    profile = build_profile(wide)
    PROFILE_OUT.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_OUT.write_text(json.dumps(profile, indent=2))
    print(f"[ok] machine profile      -> {PROFILE_OUT.relative_to(REPO)}")
    print(f"[info] native rate ~{profile['native_sample_rate_hz']} Hz, "
          f"duty cycle {profile['duty_cycle']:.1%}, "
          f"phases {profile['phases']}")
    print(f"[info] cycle ~{profile['cycle']['duration_s_mean']:.0f}s "
          f"+/- {profile['cycle']['duration_s_std']:.0f}s, "
          f"AR1={ {k: round(v, 2) for k, v in profile['ar1'].items()} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
