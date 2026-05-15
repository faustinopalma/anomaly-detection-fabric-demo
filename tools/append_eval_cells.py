"""Append Section 8 (eval dataset with injected anomalies) to notebook 06.

One-shot helper. Safe to re-run: detects whether section 8 is already
appended (by checking for the EVAL_DATASET_DIR token) and exits if so.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

NB_PATH = Path("notebooks/06_simulator_dev.ipynb")
SENTINEL = "EVAL_DATASET_DIR"


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def md(text: str) -> dict:
    return {
        "cell_type": "markdown",
        "id": new_id(),
        "metadata": {},
        "source": [l + "\n" for l in text.splitlines()],
    }


def code(text: str) -> dict:
    return {
        "cell_type": "code",
        "id": new_id(),
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": [l + "\n" for l in text.splitlines()],
    }


def main() -> None:
    nb = json.loads(NB_PATH.read_text(encoding="utf-8"))

    already = any(
        SENTINEL in "".join(c.get("source", []))
        for c in nb["cells"]
    )
    if already:
        print(f"Section 8 already present (found '{SENTINEL}'); nothing to do.")
        return

    cells = []

    cells.append(md(
"""## 8. Eval dataset with injected anomalies

The dataset under `data/training/` is **clean by design** — it is the
"normal" distribution the autoencoder will be trained on. To measure
whether the trained model actually catches degraded equipment, we now
build a **separate evaluation snapshot** that mirrors the same schema
but contains controlled anomalies with ground-truth labels.

### Train / eval separation

| Snapshot | Machines | Seed base | Contents | Used for |
|---|---|---|---|---|
| `data/training/` | M-001..M-005 | `RNG_SEED` | clean | model `fit()` |
| `data/eval/` | **M-101..M-105** | **`RNG_SEED + 1000`** | clean baseline + injected faults + labels | scoring + threshold tuning + final report |

The eval machines use a **different seed base** so that the noise
realisations are independent — no information leak from training.
2 of the 5 eval machines stay healthy as a control group; the other 3
each carry a single fault family.

### Fault catalog (3 families)

| Family | Affected sensors | Pattern | Realism notes |
|---|---|---|---|
| **bearing** | `vibration_radial` ↑↑, `vibration_axial` ↑, `temperature_bearing` ↑, `current` ↑, `power` ↑ | slow ramp + growing impulsive spikes, scaled by load | Classic spalling/pitting signature; multivariate, evolves over hours. |
| **hydraulic_leak** | `pressure_hydraulic` ↓ (ramp) or oscillating | monotonic drop *or* low-freq oscillation as pump cycles | Mono-sensor, subtle — stress-tests the model's univariate sensitivity. |
| **sensor_stuck** | one selected sensor frozen at a constant value | step (instant freeze) with `quality=0` flag | A *data* anomaly, not a *machine* anomaly — useful negative class so we can later teach the pipeline to suppress these alerts. |

All faults activate **only when the machine is running** (load > 0): a
broken bearing doesn't vibrate while the motor is OFF. Sensor freezes
are the exception — they apply throughout the chosen window.

### Episode budget

5 machines × 24 h gives plenty of room. Per faulty machine we schedule
**4 episodes** of varied severity (some obvious, some subdued) so each
family has 4 events for metric computation."""
    ))

    cells.append(md(
"""### 8.1 Build the clean eval baseline

Same simulator, different machine ids and seeds. We reuse the
`build_dataset` function from Section 7."""
    ))

    cells.append(code(
"""EVAL_DATASET_DIR = Path("../data/eval").resolve()
EVAL_DATASET_DIR.mkdir(parents=True, exist_ok=True)

EVAL_N_MACHINES   = 5
EVAL_DURATION_S   = 24 * 3600
EVAL_DT_S         = 1.0
EVAL_START_TS_UTC = datetime(2026, 5, 16, 8, 0, 0, tzinfo=timezone.utc)
EVAL_SEED_BASE    = RNG_SEED + 1000   # decouple from training noise

# Build with the same function but with a different seed base and a
# machine-id offset so eval ids are M-101..M-105.
def _build_eval_clean() -> tuple[pd.DataFrame, pd.DataFrame]:
    long_frames, wide_frames = [], []
    for i in range(EVAL_N_MACHINES):
        machine_id = f"M-{100 + i + 1:03d}"   # M-101, M-102, ...
        random.seed(EVAL_SEED_BASE + i)
        np.random.seed(EVAL_SEED_BASE + i)

        machine = Machine(machine_id=machine_id, state=State.OFF)
        df_run = simulate(machine, duration_s=EVAL_DURATION_S, dt=EVAL_DT_S)

        ts = pd.to_datetime(EVAL_START_TS_UTC) + pd.to_timedelta(df_run["t_s"], unit="s")

        wide = pd.DataFrame({
            "ts":         ts,
            "machineId":  machine_id,
            "state":      df_run["state"].astype("category"),
            "load":       df_run["load"].astype("float32"),
            **{s: df_run[s].astype("float32") for s in SENSORS},
        })
        wide_frames.append(wide)

        long = wide.melt(
            id_vars=["ts", "machineId"], value_vars=SENSORS,
            var_name="sensorId", value_name="value",
        )
        long["quality"] = np.float32(1.0)
        long = long.merge(wide[["ts", "machineId", "state"]],
                          on=["ts", "machineId"], how="left")
        long.loc[long["state"] == "OFF", "quality"] = np.float32(0.0)
        long = long.drop(columns=["state"])
        long = long[["machineId", "sensorId", "ts", "value", "quality"]]
        long_frames.append(long)

    return (pd.concat(long_frames, ignore_index=True),
            pd.concat(wide_frames, ignore_index=True))


eval_long_clean, eval_wide_clean = _build_eval_clean()
print("eval clean wide:", eval_wide_clean.shape,
      " | clean long:", eval_long_clean.shape)
print("machines:", sorted(eval_wide_clean['machineId'].unique()))"""
    ))

    cells.append(md(
"""### 8.2 Anomaly injectors

Each injector takes a *copy* of the wide DataFrame, restricts itself to
one machine and a `[onset_ts, onset_ts + duration_s]` window, and
modifies the relevant sensors in place. They all return the modified
frame plus a per-row boolean mask for the affected window so we can
rebuild ground-truth labels.

A few design choices worth flagging:

- **Load-scaling**: machine faults (bearing, hydraulic) scale with
  `wide['load']` so anomalies are essentially silent during OFF/IDLE
  and most visible during PRODUCTION_HEAVY. Realistic and forces the
  model to handle state-dependent baselines.
- **Severity ramp**: `severity_max` is reached linearly across the
  window. The very first samples after onset are barely visible — this
  is what gives a meaningful *lead-time* metric later.
- **Spikes**: bearing fault adds Poisson-timed impulse spikes on
  `vibration_radial` whose amplitude grows with severity, mimicking
  the impact-style signature of pitted rolling elements (we cannot
  resolve true BPFO/BPFI tones at 1 Hz, so we proxy with random
  impulses)."""
    ))

    cells.append(code(
'''from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class FaultSpec:
    """A scheduled anomaly episode."""
    fault_type: Literal["bearing", "hydraulic_leak", "sensor_stuck"]
    machine_id: str
    onset_ts: datetime
    duration_s: float
    severity_max: float = 1.0           # 0..1
    affected_sensor: str | None = None  # only for sensor_stuck
    pattern: str = "ramp"               # "ramp" | "oscillation" (hydraulic)
    notes: str = ""


def _window_mask(wide: pd.DataFrame, machine_id: str,
                 onset_ts: datetime, duration_s: float) -> pd.Series:
    end_ts = pd.to_datetime(onset_ts) + pd.to_timedelta(duration_s, unit="s")
    return ((wide["machineId"] == machine_id)
            & (wide["ts"] >= pd.to_datetime(onset_ts))
            & (wide["ts"] <  end_ts))


def _ramp(t_norm: np.ndarray) -> np.ndarray:
    """0 -> 1 linear ramp clipped to [0, 1]."""
    return np.clip(t_norm, 0.0, 1.0).astype(np.float32)


def inject_bearing(wide: pd.DataFrame, spec: FaultSpec,
                   rng: np.random.Generator) -> pd.Series:
    mask = _window_mask(wide, spec.machine_id, spec.onset_ts, spec.duration_s)
    if not mask.any():
        return mask

    # Fraction-of-window position 0..1 for each row inside the episode
    ts_in = wide.loc[mask, "ts"]
    t0 = pd.to_datetime(spec.onset_ts)
    pos = ((ts_in - t0).dt.total_seconds() / spec.duration_s).to_numpy()
    sev = spec.severity_max * _ramp(pos)
    load = wide.loc[mask, "load"].to_numpy(dtype=np.float32)

    # Steady-state degradation (load-scaled)
    wide.loc[mask, "vibration_radial"]   += sev * 0.40 * load
    wide.loc[mask, "vibration_axial"]    += sev * 0.15 * load
    wide.loc[mask, "temperature_bearing"]+= sev * 7.0  * load
    wide.loc[mask, "current"]            += sev * 0.60 * load
    wide.loc[mask, "power"]              += sev * 0.25 * load

    # Impulsive spikes on radial vibration (Poisson-timed, growing rate)
    n = int(mask.sum())
    base_rate = 0.005          # ~1 spike every 200 samples at sev=1
    spike_prob = base_rate * sev * load
    is_spike = rng.random(n) < spike_prob
    spike_amp = (0.6 + 0.4 * rng.random(n)) * sev * load
    radial = wide.loc[mask, "vibration_radial"].to_numpy()
    radial[is_spike] += spike_amp[is_spike]
    wide.loc[mask, "vibration_radial"] = radial.astype(np.float32)
    return mask


def inject_hydraulic_leak(wide: pd.DataFrame, spec: FaultSpec,
                          rng: np.random.Generator) -> pd.Series:
    mask = _window_mask(wide, spec.machine_id, spec.onset_ts, spec.duration_s)
    if not mask.any():
        return mask

    ts_in = wide.loc[mask, "ts"]
    t0 = pd.to_datetime(spec.onset_ts)
    pos = ((ts_in - t0).dt.total_seconds() / spec.duration_s).to_numpy()
    sev = spec.severity_max * _ramp(pos)
    load = wide.loc[mask, "load"].to_numpy(dtype=np.float32)
    pressure = wide.loc[mask, "pressure_hydraulic"].to_numpy(dtype=np.float32)

    if spec.pattern == "oscillation":
        # Pump duty-cycling: ~60 s period, amplitude grows with severity
        secs = (ts_in - t0).dt.total_seconds().to_numpy()
        osc = np.sin(2 * np.pi * secs / 60.0).astype(np.float32)
        pressure = pressure + (sev * 18.0 * osc * load)
    else:
        # Slow leak: pressure drops up to 45% under load
        pressure = pressure * (1.0 - sev * 0.45 * load)

    wide.loc[mask, "pressure_hydraulic"] = pressure.astype(np.float32)

    # Pump compensates -> small power increase
    wide.loc[mask, "power"] += (sev * 0.15 * load).astype(np.float32)
    return mask


def inject_sensor_stuck(wide: pd.DataFrame, spec: FaultSpec,
                        rng: np.random.Generator) -> pd.Series:
    if spec.affected_sensor is None:
        raise ValueError("sensor_stuck requires affected_sensor")
    mask = _window_mask(wide, spec.machine_id, spec.onset_ts, spec.duration_s)
    if not mask.any():
        return mask

    # Freeze at the value just before onset (fallback: first in-window value)
    pre = wide[(wide["machineId"] == spec.machine_id)
               & (wide["ts"] < pd.to_datetime(spec.onset_ts))]
    if len(pre) > 0:
        stuck_val = float(pre.iloc[-1][spec.affected_sensor])
    else:
        stuck_val = float(wide.loc[mask, spec.affected_sensor].iloc[0])

    wide.loc[mask, spec.affected_sensor] = np.float32(stuck_val)
    return mask


INJECTORS = {
    "bearing":         inject_bearing,
    "hydraulic_leak":  inject_hydraulic_leak,
    "sensor_stuck":    inject_sensor_stuck,
}

print("Injectors registered:", list(INJECTORS))'''
    ))

    cells.append(md(
"""### 8.3 Episode catalog and apply

Three faulty machines, four episodes each. Severity, sensor, onset and
duration vary so the eval covers a realistic spread."""
    ))

    cells.append(code(
'''def _ts(hours: float) -> datetime:
    """Helper: hours after EVAL_START_TS_UTC."""
    return EVAL_START_TS_UTC + pd.to_timedelta(hours, unit="h")


EPISODES: list[FaultSpec] = [
    # --- M-103: bearing degradation, 4 episodes of growing severity ---
    FaultSpec("bearing",        "M-103", _ts(2.0),  duration_s=1.5*3600,
              severity_max=0.30, notes="early-stage, subdued"),
    FaultSpec("bearing",        "M-103", _ts(7.0),  duration_s=2.0*3600,
              severity_max=0.55, notes="mid-stage"),
    FaultSpec("bearing",        "M-103", _ts(13.0), duration_s=2.5*3600,
              severity_max=0.80, notes="advanced"),
    FaultSpec("bearing",        "M-103", _ts(19.5), duration_s=3.0*3600,
              severity_max=1.00, notes="late-stage, near failure"),

    # --- M-104: hydraulic leak, mix of ramp and oscillation patterns ---
    FaultSpec("hydraulic_leak", "M-104", _ts(3.0),  duration_s=2.0*3600,
              severity_max=0.40, pattern="ramp",
              notes="slow leak, mild"),
    FaultSpec("hydraulic_leak", "M-104", _ts(8.5),  duration_s=1.0*3600,
              severity_max=0.70, pattern="oscillation",
              notes="pump cycling"),
    FaultSpec("hydraulic_leak", "M-104", _ts(14.0), duration_s=2.5*3600,
              severity_max=0.85, pattern="ramp",
              notes="severe leak"),
    FaultSpec("hydraulic_leak", "M-104", _ts(20.0), duration_s=2.0*3600,
              severity_max=1.00, pattern="oscillation",
              notes="failing pump"),

    # --- M-105: sensor stuck, different sensors and durations ---
    FaultSpec("sensor_stuck",   "M-105", _ts(2.5),  duration_s=0.5*3600,
              affected_sensor="temperature_motor",
              notes="short freeze"),
    FaultSpec("sensor_stuck",   "M-105", _ts(7.0),  duration_s=1.5*3600,
              affected_sensor="pressure_hydraulic",
              notes="medium freeze"),
    FaultSpec("sensor_stuck",   "M-105", _ts(12.0), duration_s=2.0*3600,
              affected_sensor="vibration_radial",
              notes="long freeze on key sensor"),
    FaultSpec("sensor_stuck",   "M-105", _ts(18.0), duration_s=3.0*3600,
              affected_sensor="current",
              notes="long freeze, electrical"),
]

# Apply episodes on a copy of the clean baseline
eval_wide = eval_wide_clean.copy()
eval_wide["fault_type"] = pd.Series(
    pd.Categorical(["normal"] * len(eval_wide),
                   categories=["normal", "bearing", "hydraulic_leak", "sensor_stuck"]))
eval_wide["is_anomaly"] = False
# Ensure category dtype can store new values
eval_wide["fault_type"] = eval_wide["fault_type"].astype(
    pd.CategoricalDtype(categories=["normal", "bearing", "hydraulic_leak", "sensor_stuck"]))

inj_rng = np.random.default_rng(EVAL_SEED_BASE + 9999)

for spec in EPISODES:
    fn = INJECTORS[spec.fault_type]
    mask = fn(eval_wide, spec, inj_rng)
    eval_wide.loc[mask, "is_anomaly"] = True
    eval_wide.loc[mask, "fault_type"] = spec.fault_type

# Episode-level labels DataFrame (one row per scheduled fault)
eval_labels = pd.DataFrame([{
    "machineId":       s.machine_id,
    "fault_type":      s.fault_type,
    "affected_sensor": s.affected_sensor,
    "onset_ts":        pd.to_datetime(s.onset_ts),
    "end_ts":          pd.to_datetime(s.onset_ts) + pd.to_timedelta(s.duration_s, unit="s"),
    "duration_s":      float(s.duration_s),
    "severity_max":    float(s.severity_max),
    "pattern":         s.pattern,
    "notes":           s.notes,
} for s in EPISODES])

print(f"Applied {len(EPISODES)} episodes")
print(f"Anomalous samples: {int(eval_wide['is_anomaly'].sum()):,} "
      f"/ {len(eval_wide):,} "
      f"({100 * eval_wide['is_anomaly'].mean():.2f}%)")
print()
print("Per-machine breakdown:")
display(eval_wide.groupby("machineId", observed=True)["fault_type"]
        .value_counts(normalize=False).unstack(fill_value=0))
print()
display(eval_labels)'''
    ))

    cells.append(md(
"""### 8.4 Persist eval files

Same parquet codec and row-group settings as the training snapshot."""
    ))

    cells.append(code(
"""# Long-form (event-shaped) view, mirroring the live KQL schema.
# Drop quality=0 mark on stuck-sensor episodes to make the dropout
# visible to downstream consumers (this is what the live pipeline
# would see if a sensor stopped reporting).
eval_long = eval_wide.melt(
    id_vars=["ts", "machineId"],
    value_vars=SENSORS,
    var_name="sensorId",
    value_name="value",
)
eval_long["quality"] = np.float32(1.0)
# OFF samples: quality 0 (machine off, no real reading)
eval_long = eval_long.merge(
    eval_wide[["ts", "machineId", "state"]],
    on=["ts", "machineId"], how="left",
)
eval_long.loc[eval_long["state"] == "OFF", "quality"] = np.float32(0.0)
# Stuck-sensor episodes: quality 0 on the affected sensor only
for spec in EPISODES:
    if spec.fault_type != "sensor_stuck":
        continue
    end_ts = pd.to_datetime(spec.onset_ts) + pd.to_timedelta(spec.duration_s, unit="s")
    sel = ((eval_long["machineId"] == spec.machine_id)
           & (eval_long["sensorId"] == spec.affected_sensor)
           & (eval_long["ts"] >= pd.to_datetime(spec.onset_ts))
           & (eval_long["ts"] <  end_ts))
    eval_long.loc[sel, "quality"] = np.float32(0.0)
eval_long = eval_long.drop(columns=["state"])
eval_long = eval_long[["machineId", "sensorId", "ts", "value", "quality"]]

EVAL_LONG_PATH   = EVAL_DATASET_DIR / "raw_telemetry.parquet"
EVAL_WIDE_PATH   = EVAL_DATASET_DIR / "telemetry_wide.parquet"
EVAL_LABELS_PATH = EVAL_DATASET_DIR / "anomaly_labels.parquet"

eval_long.to_parquet(EVAL_LONG_PATH, **PARQUET_KW)
eval_wide.to_parquet(EVAL_WIDE_PATH, **PARQUET_KW)
eval_labels.to_parquet(EVAL_LABELS_PATH, engine='pyarrow',
                       compression='zstd', compression_level=9, index=False)

for p in (EVAL_LONG_PATH, EVAL_WIDE_PATH, EVAL_LABELS_PATH):
    size_mb = p.stat().st_size / (1024 * 1024)
    print(f"{p.name:30s}  {size_mb:>8.2f} MB")"""
    ))

    cells.append(md(
"""### 8.5 Visual sanity check

For each fault family pick one episode and overlay the relevant
sensor(s) before/after injection. The shaded band marks the labelled
anomaly window."""
    ))

    cells.append(code(
'''def _plot_episode(spec: FaultSpec, sensors_to_show: list[str],
                  pad_h: float = 1.5):
    """Plot the chosen sensors around an episode, with the window shaded."""
    pad = pd.to_timedelta(pad_h, unit="h")
    t0 = pd.to_datetime(spec.onset_ts) - pad
    t1 = pd.to_datetime(spec.onset_ts) + pd.to_timedelta(spec.duration_s, unit="s") + pad

    sub_clean = eval_wide_clean[(eval_wide_clean["machineId"] == spec.machine_id)
                                & (eval_wide_clean["ts"].between(t0, t1))]
    sub_anom  = eval_wide[(eval_wide["machineId"] == spec.machine_id)
                          & (eval_wide["ts"].between(t0, t1))]

    fig, axes = plt.subplots(len(sensors_to_show), 1,
                             figsize=(13, 1.8 * len(sensors_to_show)),
                             sharex=True)
    if len(sensors_to_show) == 1:
        axes = [axes]
    for ax, sensor in zip(axes, sensors_to_show):
        ax.plot(sub_clean["ts"], sub_clean[sensor],
                lw=0.7, color="tab:gray", alpha=0.7, label="clean")
        ax.plot(sub_anom["ts"],  sub_anom[sensor],
                lw=0.8, color="tab:red", label="with anomaly")
        ax.axvspan(pd.to_datetime(spec.onset_ts),
                   pd.to_datetime(spec.onset_ts) + pd.to_timedelta(spec.duration_s, unit="s"),
                   color="orange", alpha=0.15, label="labelled window")
        ax.set_ylabel(sensor, fontsize=8)
        ax.legend(loc="upper left", fontsize=7)
    axes[0].set_title(f"{spec.fault_type} on {spec.machine_id} "
                      f"(severity_max={spec.severity_max}, "
                      f"pattern={spec.pattern})", fontsize=10)
    axes[-1].set_xlabel("time")
    plt.tight_layout()
    plt.show()


# Pick one episode per family
ep_bearing    = next(s for s in EPISODES if s.fault_type == "bearing"
                                            and s.severity_max >= 0.7)
ep_hydraulic  = next(s for s in EPISODES if s.fault_type == "hydraulic_leak"
                                            and s.pattern == "ramp"
                                            and s.severity_max >= 0.7)
ep_stuck      = next(s for s in EPISODES if s.fault_type == "sensor_stuck"
                                            and s.duration_s >= 1.5*3600)

_plot_episode(ep_bearing,
              ["vibration_radial", "vibration_axial",
               "temperature_bearing", "current"])
_plot_episode(ep_hydraulic,
              ["pressure_hydraulic", "power"])
_plot_episode(ep_stuck,
              [ep_stuck.affected_sensor])'''
    ))

    cells.append(md(
"""### 8.6 What the training notebook will load

```python
import pandas as pd

# Training (clean) — for model.fit()
train = pd.read_parquet("../data/training/telemetry_wide.parquet")

# Evaluation (with anomalies) — for model.predict() and metrics
eval_df  = pd.read_parquet("../data/eval/telemetry_wide.parquet")
labels   = pd.read_parquet("../data/eval/anomaly_labels.parquet")

# Per-row ground truth is already on eval_df['is_anomaly']
# Per-episode metadata (severity, pattern, sensor) is on labels
```

In Fabric the equivalent eval comes from running the same injector on a
KQL snapshot of the live `raw_telemetry` table — same schema, same
labels DataFrame, no code change."""
    ))

    nb["cells"].extend(cells)
    NB_PATH.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    print(f"OK - notebook now has {len(nb['cells'])} cells "
          f"(+{len(cells)} for Section 8)")


if __name__ == "__main__":
    main()
