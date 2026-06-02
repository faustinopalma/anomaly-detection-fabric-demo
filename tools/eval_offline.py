"""Offline supervised evaluation of a per-machine anomaly model.

Generates *labelled* synthetic telemetry by reusing the repo's own physics /
empirical engines, injects plausible anomalies (spike / drift / stuck) on known
time spans, scores the exported ONNX model, and reports detection metrics at the
deployed threshold plus threshold-independent ROC-AUC and PR-AUC.

This is a purely offline harness: it never touches the Fabric environment. Train
and serve stay consistent because the normal telemetry comes from the same
generators used for training, and inputs are normalised with the per-machine
scaler stored in the model's ``metadata.json``.

Usage:
    python tools/eval_offline.py --machine M-004 --hours 6 --episodes 60
    python tools/eval_offline.py --machine M-003 --hours 8 --episodes 60
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cloud-training" / "src"))
sys.path.insert(0, str(REPO / "simulator-local"))

WINDOW = 64


# ---------------------------------------------------------------------------
# Normal-telemetry generators (reuse the repo engines, no Fabric, no torch).
# ---------------------------------------------------------------------------
def generate_normal(machine: str, hours: float, seed: int, sensors: list[str]):
    """Return ``(values[T, F], active_mask[T])`` for the requested machine.

    Sensor columns are returned in the order requested (the model scaler order).
    ``active_mask`` marks ticks where injecting an anomaly is physically
    meaningful (machine running / cutting).
    """
    n_seconds = int(hours * 3600)
    if machine == "M-003":
        from cnc_engine import generate_frame, load_profile

        profile = load_profile(REPO / "data" / "cnc_profile_M-003.json")
        vals, mask = generate_frame(profile, n_seconds, seed)
        order = [profile["sensors"].index(s) for s in sensors]
        return vals[:, order].astype(np.float64), mask

    # FSM physics machines (M-001/M-002/M-004) share the 8-sensor engine.
    from generate_and_train import generate_wide

    df = generate_wide(machine, hours=hours, seed=seed)
    vals = np.ascontiguousarray(df[sensors].to_numpy(dtype=np.float64))
    mask = (df["state"].to_numpy() != "OFF")
    return vals, mask


# ---------------------------------------------------------------------------
# Anomaly injection -> ground-truth labels.
# ---------------------------------------------------------------------------
def inject_anomalies(values, sigma, active_mask, n_episodes, rng):
    """Inject labelled anomalies in place. Returns ``(labels[T], episodes)``.

    Magnitudes are anchored to each sensor's std (``sigma``) so injections stay
    plausible across very different sensor scales (e.g. % vs rpm).
    """
    T, F = values.shape
    labels = np.zeros(T, dtype=bool)
    episodes: list[dict] = []
    kinds = ["spike", "drift", "stuck"]
    attempts = 0
    while len(episodes) < n_episodes and attempts < n_episodes * 80:
        attempts += 1
        kind = kinds[int(rng.integers(len(kinds)))]
        sensor = int(rng.integers(F))
        if kind == "spike":
            dur = int(rng.integers(2, 5))
        elif kind == "drift":
            dur = int(rng.integers(12, 28))
        else:  # stuck
            dur = int(rng.integers(8, 22))
        start = int(rng.integers(0, T - dur))
        span = slice(start, start + dur)
        if active_mask is not None and not active_mask[span].all():
            continue
        m0, m1 = max(0, start - 3), min(T, start + dur + 3)
        if labels[m0:m1].any():
            continue  # keep episodes non-overlapping with a small margin

        s = sigma[sensor]
        sign = 1.0 if rng.random() < 0.5 else -1.0
        if kind == "spike":
            values[span, sensor] += sign * rng.uniform(3.0, 5.0) * s
        elif kind == "drift":
            ramp = np.linspace(0.0, 1.0, dur)
            values[span, sensor] += sign * rng.uniform(3.0, 4.5) * s * ramp
        else:  # stuck: freeze the sensor at its value when the fault began
            values[span, sensor] = values[start, sensor]
        labels[span] = True
        episodes.append({"start": start, "dur": dur, "kind": kind, "sensor": sensor})
    return labels, episodes


# ---------------------------------------------------------------------------
# Windowing + scoring.
# ---------------------------------------------------------------------------
def make_windows(arr, win=WINDOW):
    sw = np.lib.stride_tricks.sliding_window_view(arr, win, axis=0)  # (N, F, win)
    return np.ascontiguousarray(sw.transpose(0, 2, 1))  # (N, win, F)


def window_labels(labels, win=WINDOW):
    sw = np.lib.stride_tricks.sliding_window_view(labels, win)  # (N, win)
    return sw.any(axis=1)


def score_onnx(model_path, windows_norm):
    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    return sess.run(["score"], {"window": windows_norm.astype(np.float32)})[0].ravel()


# ---------------------------------------------------------------------------
# Metrics (numpy only; no scikit-learn dependency).
# ---------------------------------------------------------------------------
def _rankdata(a):
    order = a.argsort()
    ranks = np.empty(len(a), dtype=np.float64)
    ranks[order] = np.arange(1, len(a) + 1)
    sa = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    return ranks


def roc_auc(scores, labels):
    pos = labels.sum()
    neg = (~labels).sum()
    if pos == 0 or neg == 0:
        return float("nan")
    r = _rankdata(scores)
    return float((r[labels].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def average_precision(scores, labels):
    if labels.sum() == 0:
        return float("nan")
    order = scores.argsort()[::-1]
    y = labels[order].astype(np.int64)
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / y.sum()
    r_prev = np.concatenate(([0.0], recall[:-1]))
    return float(np.sum((recall - r_prev) * precision))


def threshold_metrics(scores, labels, thr):
    pred = scores > thr
    tp = int((pred & labels).sum())
    fp = int((pred & ~labels).sum())
    fn = int((~pred & labels).sum())
    tn = int((~pred & ~labels).sum())
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if prec and rec and not np.isnan(prec) and not np.isnan(rec) else float("nan")
    spec = tn / (tn + fp) if (tn + fp) else float("nan")
    acc = (tp + tn) / (tp + tn + fp + fn)
    bal = (rec + spec) / 2 if not (np.isnan(rec) or np.isnan(spec)) else float("nan")
    fpr = fp / (fp + tn) if (fp + tn) else float("nan")
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=prec, recall=rec, f1=f1,
                specificity=spec, accuracy=acc, balanced_accuracy=bal, fpr=fpr)


def episode_recall(scores, win_starts_fired, episodes):
    """Episode-level detection: an episode is detected if any window that
    overlaps it fires. ``win_starts_fired`` is a boolean array indexed by window
    start position (length N). Returns overall + per-kind recall."""
    n_win = len(win_starts_fired)
    by_kind: dict[str, list[bool]] = {}
    detected_all = 0
    for ep in episodes:
        lo = max(0, ep["start"] - (WINDOW - 1))
        hi = min(n_win, ep["start"] + ep["dur"])  # window starts overlapping span
        hit = bool(win_starts_fired[lo:hi].any()) if hi > lo else False
        detected_all += int(hit)
        by_kind.setdefault(ep["kind"], []).append(hit)
    overall = detected_all / len(episodes) if episodes else float("nan")
    per_kind = {k: (sum(v) / len(v), len(v)) for k, v in sorted(by_kind.items())}
    return overall, per_kind


# ---------------------------------------------------------------------------
def evaluate(machine: str, hours: float, episodes: int, seed: int, precision: str):
    model_dir = REPO / "models" / f"transformer_ae_small__{machine}"
    meta = json.loads((model_dir / "metadata.json").read_text())
    sensors = meta["scaler"]["sensors"]
    mean = np.asarray(meta["scaler"]["mean"], dtype=np.float64)
    std = np.asarray(meta["scaler"]["std"], dtype=np.float64)
    thr = float(meta["threshold"])
    model_path = str(model_dir / ("model.fp16.onnx" if precision == "fp16" else "model.onnx"))

    rng = np.random.default_rng(seed)
    values, active = generate_normal(machine, hours, seed, sensors)
    labels, eps = inject_anomalies(values, std, active, episodes, rng)

    norm = (values - mean) / std
    windows = make_windows(norm)
    win_lbl = window_labels(labels)
    scores = score_onnx(model_path, windows)

    fired = scores > thr
    m = threshold_metrics(scores, win_lbl, thr)
    auc = roc_auc(scores, win_lbl)
    ap = average_precision(scores, win_lbl)
    ep_rec, per_kind = episode_recall(scores, fired, eps)

    print(f"\n===== {machine}  (model {precision}, threshold={thr:.4f}) =====")
    print(f"telemetry: {len(values):,} ticks ({hours} h) | windows: {len(scores):,} "
          f"| injected episodes: {len(eps)} | anomalous windows: {int(win_lbl.sum()):,} "
          f"({win_lbl.mean():.1%})")
    print("\n-- window-level metrics @ deployed threshold --")
    print(f"  precision          : {m['precision']:.3f}")
    print(f"  recall (sensitivity): {m['recall']:.3f}")
    print(f"  F1                 : {m['f1']:.3f}")
    print(f"  specificity        : {m['specificity']:.3f}")
    print(f"  balanced accuracy  : {m['balanced_accuracy']:.3f}")
    print(f"  false-positive rate: {m['fpr']:.4f}")
    print(f"  confusion (TP/FP/FN/TN): {m['tp']} / {m['fp']} / {m['fn']} / {m['tn']}")
    print("\n-- threshold-independent --")
    print(f"  ROC-AUC            : {auc:.4f}")
    print(f"  PR-AUC (avg prec)  : {ap:.4f}")
    print("\n-- episode-level detection recall --")
    print(f"  overall            : {ep_rec:.3f}  ({sum(1 for e in eps)} episodes)")
    for k, (r, n) in per_kind.items():
        print(f"    {k:<7s}: {r:.3f}  (n={n})")
    print("\n-- score separation --")
    if win_lbl.any():
        print(f"  normal  windows score: median={np.median(scores[~win_lbl]):.4f}  "
              f"p99={np.percentile(scores[~win_lbl], 99):.4f}")
        print(f"  anomaly windows score: median={np.median(scores[win_lbl]):.4f}  "
              f"max={scores[win_lbl].max():.4f}")

    return {
        "machine": machine, "precision": precision, "threshold": thr,
        "hours": hours, "seed": seed, "episodes": len(eps),
        "n_windows": int(len(scores)), "anomalous_windows": int(win_lbl.sum()),
        "window_metrics": m, "roc_auc": auc, "pr_auc": ap,
        "episode_recall_overall": ep_rec,
        "episode_recall_by_kind": {k: {"recall": r, "n": n} for k, (r, n) in per_kind.items()},
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--machine", nargs="+", default=["M-004"],
                    help="machine id(s) with a trained model, e.g. M-003 M-004")
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--episodes", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260602)
    ap.add_argument("--precision", choices=["fp32", "fp16"], default="fp32",
                    help="fp32 = model.onnx (matches threshold), fp16 = Kusto-deployed model")
    ap.add_argument("--out", type=str, default=None, help="optional JSON summary path")
    args = ap.parse_args(argv)

    results = [evaluate(m, args.hours, args.episodes, args.seed, args.precision)
               for m in args.machine]
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\n[saved] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
