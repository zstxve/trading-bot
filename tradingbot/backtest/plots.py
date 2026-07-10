"""Report charts: equity vs benchmark, drawdown, monthly returns, trade
distribution. Saved as PNGs; nothing interactive."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from tradingbot.portfolio import Trade


def save_report_plots(
    outdir: Path,
    equity: pd.Series,
    benchmark: Optional[pd.Series],
    trades: List[Trade],
    prefix: str = "",
) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    tag = f"{prefix}_" if prefix else ""

    # --- equity vs benchmark (both normalised to 1.0) -----------------------
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(equity.index, equity / equity.iloc[0], label="strategy", lw=1.2)
    if benchmark is not None and len(benchmark) > 1:
        b = benchmark[benchmark.index >= equity.index[0]]
        if len(b) > 1:
            ax.plot(b.index, b / b.iloc[0], label="buy & hold (equal weight)",
                    lw=1.0, alpha=0.75)
    ax.set_title(f"{prefix} equity (normalised)".strip())
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(outdir / f"{tag}equity.png", dpi=120)
    plt.close(fig)

    # --- drawdown ------------------------------------------------------------
    dd = equity / equity.cummax() - 1.0
    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.fill_between(dd.index, dd * 100, 0, color="tab:red", alpha=0.4)
    ax.set_title(f"{prefix} drawdown (%)".strip())
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(outdir / f"{tag}drawdown.png", dpi=120)
    plt.close(fig)

    # --- monthly returns heatmap ----------------------------------------------
    monthly = equity.resample("ME").last().pct_change().dropna()
    if len(monthly) >= 2:
        table = pd.DataFrame({
            "year": monthly.index.year, "month": monthly.index.month, "ret": monthly.values
        }).pivot(index="year", columns="month", values="ret")
        fig, ax = plt.subplots(figsize=(11, 1.2 + 0.5 * len(table)))
        lim = max(abs(np.nanmin(table.values)), abs(np.nanmax(table.values)), 1e-9)
        im = ax.imshow(table.values, cmap="RdYlGn", vmin=-lim, vmax=lim, aspect="auto")
        ax.set_xticks(range(len(table.columns)), [f"{m:02d}" for m in table.columns])
        ax.set_yticks(range(len(table.index)), table.index)
        for yi in range(table.shape[0]):
            for xi in range(table.shape[1]):
                v = table.values[yi, xi]
                if not np.isnan(v):
                    ax.text(xi, yi, f"{v:.1%}", ha="center", va="center", fontsize=8)
        ax.set_title(f"{prefix} monthly returns".strip())
        fig.colorbar(im, ax=ax, shrink=0.8)
        fig.tight_layout()
        fig.savefig(outdir / f"{tag}monthly_returns.png", dpi=120)
        plt.close(fig)

    # --- trade distribution ----------------------------------------------------
    if trades:
        rets = [t.return_pct * 100 for t in trades]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(rets, bins=min(40, max(10, len(rets) // 3)), color="tab:blue", alpha=0.8)
        ax.axvline(0, color="black", lw=1)
        ax.set_title(f"{prefix} trade return distribution (%)".strip())
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(outdir / f"{tag}trade_distribution.png", dpi=120)
        plt.close(fig)
