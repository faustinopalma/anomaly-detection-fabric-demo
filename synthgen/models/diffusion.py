"""Conditional 1-D diffusion model (DDPM) for multivariate spindle signals.

Self-contained (only ``torch`` + ``numpy``) so the Azure ML training entrypoint
can import *this file alone*. It learns to denoise windows of shape
``[B, C, L]`` (C = 3 signals, L = window length) conditioned on the discrete
machine regime (``fase``) of the window. A small 1-D residual CNN with FiLM
conditioning predicts the added noise (the standard DDPM ε-objective). An
optional *physics* penalty keeps the load/power/torque cross-correlation of the
denoised estimate close to the real one, enforcing the known coupling.

The same class trains in seconds for a couple of epochs on CPU (local
methodology loops) and scales to hundreds of epochs on a GPU (Azure ML).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class DiffusionConfig:
    channels: int = 3
    length: int = 128
    base_width: int = 64
    n_res_blocks: int = 2
    regime_embed_dim: int = 16
    n_regimes: int = 32
    timesteps: int = 200
    beta_start: float = 1e-4
    beta_end: float = 0.02
    lr: float = 2e-4
    batch_size: int = 64
    epochs: int = 2
    physics_lambda: float = 0.1
    seed: int = 7


def _timestep_embedding(t: torch.Tensor, dim: int) -> torch.Tensor:
    """Sinusoidal embedding of integer diffusion steps ``t`` -> ``[B, dim]``."""
    half = dim // 2
    freqs = torch.exp(
        -math.log(10000) * torch.arange(half, device=t.device, dtype=torch.float32) / max(1, half)
    )
    args = t.float()[:, None] * freqs[None]
    emb = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
    if dim % 2:
        emb = F.pad(emb, (0, 1))
    return emb


class _FiLMResBlock(nn.Module):
    """1-D residual conv block modulated (scale/shift) by the conditioning vector."""

    def __init__(self, width: int, cond_dim: int):
        super().__init__()
        self.conv1 = nn.Conv1d(width, width, kernel_size=5, padding=2)
        self.conv2 = nn.Conv1d(width, width, kernel_size=5, padding=2)
        self.norm1 = nn.GroupNorm(8, width)
        self.norm2 = nn.GroupNorm(8, width)
        self.film = nn.Linear(cond_dim, 2 * width)

    def forward(self, x: torch.Tensor, cond: torch.Tensor) -> torch.Tensor:
        h = self.conv1(F.silu(self.norm1(x)))
        scale, shift = self.film(cond)[:, :, None].chunk(2, dim=1)
        h = h * (1 + scale) + shift
        h = self.conv2(F.silu(self.norm2(h)))
        return x + h


class _DenoiseNet(nn.Module):
    def __init__(self, cfg: DiffusionConfig):
        super().__init__()
        self.cfg = cfg
        w = cfg.base_width
        self.t_dim = w
        cond_dim = w + cfg.regime_embed_dim
        self.regime_emb = nn.Embedding(cfg.n_regimes, cfg.regime_embed_dim)
        self.t_mlp = nn.Sequential(nn.Linear(w, w), nn.SiLU(), nn.Linear(w, w))
        self.in_proj = nn.Conv1d(cfg.channels, w, kernel_size=1)
        self.blocks = nn.ModuleList(
            [_FiLMResBlock(w, cond_dim) for _ in range(cfg.n_res_blocks)]
        )
        self.out_proj = nn.Conv1d(w, cfg.channels, kernel_size=1)

    def forward(self, x: torch.Tensor, t: torch.Tensor, regime: torch.Tensor) -> torch.Tensor:
        temb = self.t_mlp(_timestep_embedding(t, self.t_dim))
        remb = self.regime_emb(regime.clamp(0, self.cfg.n_regimes - 1))
        cond = torch.cat([temb, remb], dim=-1)
        h = self.in_proj(x)
        for blk in self.blocks:
            h = blk(h, cond)
        return self.out_proj(h)


class ConditionalDiffusion:
    """DDPM wrapper: ``fit`` on windows, ``sample`` conditioned on regimes."""

    def __init__(self, cfg: DiffusionConfig, device: str = "cpu"):
        self.cfg = cfg
        self.device = torch.device(device)
        torch.manual_seed(cfg.seed)
        np.random.seed(cfg.seed)
        self.net = _DenoiseNet(cfg).to(self.device)
        betas = torch.linspace(cfg.beta_start, cfg.beta_end, cfg.timesteps)
        self.betas = betas.to(self.device)
        self.alphas = (1.0 - betas).to(self.device)
        self.alpha_bars = torch.cumprod(self.alphas, dim=0).to(self.device)
        self._target_corr: torch.Tensor | None = None

    # ---- training --------------------------------------------------------
    def _q_sample(self, x0: torch.Tensor, t: torch.Tensor, noise: torch.Tensor) -> torch.Tensor:
        ab = self.alpha_bars[t][:, None, None]
        return torch.sqrt(ab) * x0 + torch.sqrt(1 - ab) * noise

    def _x0_from_eps(self, xt: torch.Tensor, t: torch.Tensor, eps: torch.Tensor) -> torch.Tensor:
        ab = self.alpha_bars[t][:, None, None]
        return (xt - torch.sqrt(1 - ab) * eps) / torch.sqrt(ab)

    @staticmethod
    def _batch_corr(x: torch.Tensor) -> torch.Tensor:
        """Mean cross-channel correlation matrix over a batch of ``[B, C, L]``."""
        b, c, _ = x.shape
        xm = x - x.mean(dim=2, keepdim=True)
        std = xm.std(dim=2, keepdim=True) + 1e-6
        xn = xm / std
        corr = torch.einsum("bcl,bdl->bcd", xn, xn) / x.shape[2]
        return corr.mean(dim=0)

    def fit(
        self,
        windows: np.ndarray,
        regimes: np.ndarray,
        *,
        log_every: int = 50,
        progress: bool = True,
    ) -> list[float]:
        """Train on normalized windows ``[N, L, C]`` and regime ids ``[N]``.

        Returns the per-epoch mean loss history.
        """
        cfg = self.cfg
        x = torch.tensor(np.transpose(windows, (0, 2, 1)), dtype=torch.float32)  # [N,C,L]
        r = torch.tensor(regimes, dtype=torch.long)
        # Precompute the real cross-channel correlation as the physics target.
        if x.shape[0] > 0:
            self._target_corr = self._batch_corr(x.to(self.device)).detach()
        opt = torch.optim.Adam(self.net.parameters(), lr=cfg.lr)
        n = x.shape[0]
        history: list[float] = []
        self.net.train()
        for epoch in range(cfg.epochs):
            perm = torch.randperm(n)
            losses: list[float] = []
            for i in range(0, n, cfg.batch_size):
                idx = perm[i : i + cfg.batch_size]
                x0 = x[idx].to(self.device)
                reg = r[idx].to(self.device)
                t = torch.randint(0, cfg.timesteps, (x0.shape[0],), device=self.device)
                noise = torch.randn_like(x0)
                xt = self._q_sample(x0, t, noise)
                eps = self.net(xt, t, reg)
                loss = F.mse_loss(eps, noise)
                if cfg.physics_lambda > 0 and self._target_corr is not None:
                    x0_hat = self._x0_from_eps(xt, t, eps)
                    corr = self._batch_corr(x0_hat)
                    loss = loss + cfg.physics_lambda * F.mse_loss(corr, self._target_corr)
                opt.zero_grad()
                loss.backward()
                opt.step()
                losses.append(float(loss.item()))
            mean = float(np.mean(losses)) if losses else float("nan")
            history.append(mean)
            if progress:
                print(f"[diffusion] epoch {epoch + 1}/{cfg.epochs} loss={mean:.4f}")
        return history

    # ---- sampling --------------------------------------------------------
    @torch.no_grad()
    def sample(self, regimes: np.ndarray, seed: int | None = None) -> np.ndarray:
        """Ancestral sampling. ``regimes`` is ``[B]`` (one id per window).

        Returns normalized windows ``[B, L, C]``.
        """
        cfg = self.cfg
        if seed is not None:
            torch.manual_seed(seed)
        self.net.eval()
        b = len(regimes)
        reg = torch.tensor(regimes, dtype=torch.long, device=self.device)
        x = torch.randn(b, cfg.channels, cfg.length, device=self.device)
        for step in reversed(range(cfg.timesteps)):
            t = torch.full((b,), step, dtype=torch.long, device=self.device)
            eps = self.net(x, t, reg)
            alpha = self.alphas[step]
            ab = self.alpha_bars[step]
            coef = (1 - alpha) / torch.sqrt(1 - ab)
            mean = (x - coef * eps) / torch.sqrt(alpha)
            if step > 0:
                x = mean + torch.sqrt(self.betas[step]) * torch.randn_like(x)
            else:
                x = mean
        return np.transpose(x.cpu().numpy(), (0, 2, 1))  # [B, L, C]

    # ---- persistence -----------------------------------------------------
    def save(self, path: str) -> None:
        torch.save({"cfg": self.cfg.__dict__, "state_dict": self.net.state_dict()}, path)

    @classmethod
    def load(cls, path: str, device: str = "cpu") -> "ConditionalDiffusion":
        ckpt = torch.load(path, map_location=device)
        cfg = DiffusionConfig(**ckpt["cfg"])
        obj = cls(cfg, device=device)
        obj.net.load_state_dict(ckpt["state_dict"])
        return obj
