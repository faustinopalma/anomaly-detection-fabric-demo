"""Fidelity metrics — the "judge" that quantifies how realistic synthetic data is.

Defined *before* any model so every generator is scored the same way. Covers the
four axes that matter for this telemetry:

1. Marginals   — KS distance & 1-D Wasserstein per signal.
2. Dependencies — correlation-matrix error + lagged cross-correlation, ACF, PSD.
3. Regimes     — ``fase`` dwell-time distributions and transition matrix error.
4. Timing      — inter-arrival gap distribution distance.

Plus the two learning-based scores from the TimeGAN literature:
- discriminative score : a classifier's ability to tell real from synthetic
                         (0.5 = indistinguishable → good).
- predictive score     : train-on-synthetic / test-on-real next-step MAE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

try:  # optional, used by a couple of metrics
    from scipy import signal as _sps
    from scipy.stats import ks_2samp, wasserstein_distance
    _HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    _HAVE_SCIPY = False


def _ks(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if a.size == 0 or b.size == 0:
        return float("nan")
    if _HAVE_SCIPY:
        return float(ks_2samp(a, b).statistic)
    # Fallback: empirical CDF sup-distance on a shared grid.
    grid = np.linspace(min(a.min(), b.min()), max(a.max(), b.max()), 512)
    fa = np.searchsorted(np.sort(a), grid, side="right") / a.size
    fb = np.searchsorted(np.sort(b), grid, side="right") / b.size
    return float(np.max(np.abs(fa - fb)))


def _wass(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if a.size == 0 or b.size == 0:
        return float("nan")
    if _HAVE_SCIPY:
        return float(wasserstein_distance(a, b))
    # Fallback: L1 distance between sorted quantiles.
    q = np.linspace(0, 1, 256)
    return float(np.mean(np.abs(np.quantile(a, q) - np.quantile(b, q))))


def marginal_scores(real: np.ndarray, synth: np.ndarray, names: list[str]) -> dict[str, dict[str, float]]:
    """Per-channel KS and Wasserstein. Arrays are ``[N, C]``."""
    out: dict[str, dict[str, float]] = {}
    for c, name in enumerate(names):
        out[name] = {
            "ks": _ks(real[:, c], synth[:, c]),
            "wasserstein": _wass(real[:, c], synth[:, c]),
        }
    return out


def correlation_error(real: np.ndarray, synth: np.ndarray) -> float:
    """Frobenius norm of the difference of channel correlation matrices."""
    cr = np.corrcoef(np.nan_to_num(real).T)
    cs = np.corrcoef(np.nan_to_num(synth).T)
    return float(np.linalg.norm(cr - cs))


def cross_correlation(x: np.ndarray, i: int, j: int, max_lag: int = 50) -> np.ndarray:
    """Normalized cross-correlation between channels ``i`` and ``j`` over lags."""
    a = np.nan_to_num(x[:, i]) - np.nanmean(x[:, i])
    b = np.nan_to_num(x[:, j]) - np.nanmean(x[:, j])
    denom = (np.std(a) * np.std(b) * len(a)) or 1.0
    full = np.correlate(a, b, mode="full") / denom
    mid = len(full) // 2
    return full[mid - max_lag : mid + max_lag + 1]


def acf(x: np.ndarray, nlags: int = 50) -> np.ndarray:
    """Autocorrelation of a 1-D series up to ``nlags``."""
    x = np.nan_to_num(x) - np.nanmean(x)
    v = np.var(x) or 1.0
    out = np.empty(nlags + 1)
    for k in range(nlags + 1):
        out[k] = np.mean(x[: len(x) - k] * x[k:]) / v if len(x) > k else 0.0
    return out


def psd(x: np.ndarray, fs: float = 5.0):
    """Power spectral density (Welch). ``fs`` defaults to 5 Hz (200 ms grid)."""
    x = np.nan_to_num(x)
    if _HAVE_SCIPY:
        f, p = _sps.welch(x, fs=fs, nperseg=min(256, len(x)))
        return f, p
    # Fallback: periodogram magnitude.
    p = np.abs(np.fft.rfft(x)) ** 2 / len(x)
    f = np.fft.rfftfreq(len(x), d=1.0 / fs)
    return f, p


def regime_durations(reg: np.ndarray) -> np.ndarray:
    """Dwell times (in samples) of constant-``fase`` runs."""
    if reg.size == 0:
        return np.empty(0)
    change = np.flatnonzero(np.diff(reg) != 0)
    bounds = np.concatenate(([-1], change, [len(reg) - 1]))
    return np.diff(bounds)


def transition_matrix(reg: np.ndarray, n_states: int) -> np.ndarray:
    """Row-normalized ``fase`` transition matrix over ``n_states``."""
    m = np.zeros((n_states, n_states))
    for a, b in zip(reg[:-1], reg[1:]):
        if 0 <= a < n_states and 0 <= b < n_states:
            m[a, b] += 1
    rs = m.sum(axis=1, keepdims=True)
    rs[rs == 0] = 1.0
    return m / rs


def discriminative_score(real: np.ndarray, synth: np.ndarray, seed: int = 0) -> float:
    """|accuracy - 0.5| of a classifier separating real vs synthetic windows.

    Lower is better (0 → indistinguishable). Flattens windows to feature
    vectors; uses a small random-forest if sklearn is present, else logistic.
    """
    rng = np.random.default_rng(seed)
    xr = real.reshape(len(real), -1)
    xs = synth.reshape(len(synth), -1)
    n = min(len(xr), len(xs))
    if n < 10:
        return float("nan")
    xr, xs = xr[rng.permutation(len(xr))[:n]], xs[rng.permutation(len(xs))[:n]]
    X = np.nan_to_num(np.vstack([xr, xs]))
    y = np.concatenate([np.ones(n), np.zeros(n)])
    perm = rng.permutation(2 * n)
    X, y = X[perm], y[perm]
    cut = int(0.7 * len(X))
    try:
        from sklearn.ensemble import RandomForestClassifier

        clf = RandomForestClassifier(n_estimators=100, random_state=seed, n_jobs=-1)
        clf.fit(X[:cut], y[:cut])
        acc = float(clf.score(X[cut:], y[cut:]))
    except Exception:  # noqa: BLE001
        # Nearest-centroid fallback.
        mu1 = X[:cut][y[:cut] == 1].mean(0)
        mu0 = X[:cut][y[:cut] == 0].mean(0)
        pred = (np.linalg.norm(X[cut:] - mu1, axis=1) < np.linalg.norm(X[cut:] - mu0, axis=1))
        acc = float(np.mean(pred == (y[cut:] == 1)))
    return abs(acc - 0.5)


def predictive_score(real: np.ndarray, synth: np.ndarray, seed: int = 0) -> float:
    """Train-on-synthetic / test-on-real one-step-ahead MAE (lower is better).

    A tiny linear AR(1)-style predictor per channel, fit on synthetic windows
    and evaluated on real windows, averaged over channels.
    """
    def fit_eval(train: np.ndarray, test: np.ndarray) -> float:
        # Predict next step from current step, per channel, least squares.
        maes = []
        for c in range(train.shape[2]):
            xt = train[:, :-1, c].reshape(-1)
            yt = train[:, 1:, c].reshape(-1)
            m = np.isfinite(xt) & np.isfinite(yt)
            if m.sum() < 5:
                continue
            A = np.vstack([xt[m], np.ones(m.sum())]).T
            coef, *_ = np.linalg.lstsq(A, yt[m], rcond=None)
            xe = test[:, :-1, c].reshape(-1)
            ye = test[:, 1:, c].reshape(-1)
            me = np.isfinite(xe) & np.isfinite(ye)
            pred = coef[0] * xe[me] + coef[1]
            maes.append(float(np.mean(np.abs(pred - ye[me]))))
        return float(np.mean(maes)) if maes else float("nan")

    return fit_eval(synth, real)


@dataclass
class FidelityReport:
    marginals: dict[str, dict[str, float]] = field(default_factory=dict)
    correlation_error: float = float("nan")
    discriminative: float = float("nan")
    predictive: float = float("nan")
    regime_duration_ks: float = float("nan")
    transition_error: float = float("nan")
    gap_ks: float = float("nan")
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, float]:
        marg_ks = np.nanmean([m["ks"] for m in self.marginals.values()]) if self.marginals else float("nan")
        return {
            "marginal_ks_mean": float(marg_ks),
            "correlation_error": self.correlation_error,
            "discriminative": self.discriminative,
            "predictive": self.predictive,
            "regime_duration_ks": self.regime_duration_ks,
            "transition_error": self.transition_error,
            "gap_ks": self.gap_ks,
        }


def fidelity_report(
    real_flat: np.ndarray,
    synth_flat: np.ndarray,
    names: list[str],
    *,
    real_windows: np.ndarray | None = None,
    synth_windows: np.ndarray | None = None,
    real_regime: np.ndarray | None = None,
    synth_regime: np.ndarray | None = None,
    n_states: int = 32,
    real_gaps: np.ndarray | None = None,
    synth_gaps: np.ndarray | None = None,
    seed: int = 0,
) -> FidelityReport:
    """Compute the full fidelity scorecard. ``*_flat`` are ``[N, C]`` samples."""
    rep = FidelityReport()
    rep.marginals = marginal_scores(real_flat, synth_flat, names)
    rep.correlation_error = correlation_error(real_flat, synth_flat)
    if real_windows is not None and synth_windows is not None:
        rep.discriminative = discriminative_score(real_windows, synth_windows, seed)
        rep.predictive = predictive_score(real_windows, synth_windows, seed)
    if real_regime is not None and synth_regime is not None:
        rd = regime_durations(real_regime)
        sd = regime_durations(synth_regime)
        rep.regime_duration_ks = _ks(rd.astype(float), sd.astype(float))
        tr = transition_matrix(real_regime, n_states)
        ts = transition_matrix(synth_regime, n_states)
        rep.transition_error = float(np.linalg.norm(tr - ts))
    if real_gaps is not None and synth_gaps is not None:
        rep.gap_ks = _ks(real_gaps, synth_gaps)
    return rep
