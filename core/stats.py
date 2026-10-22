"""Wilson interval, McNemar on discordant items, paired cluster-bootstrap difference.

References: Wilson (1927); McNemar (1947); Miller, "Adding error bars to evals" (arXiv 2411.00640).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import binomtest, norm
from statsmodels.stats.proportion import proportion_confint


@dataclass(frozen=True)
class Accuracy:
    n: int
    k: int
    acc: float
    lo: float
    hi: float
    alpha: float = 0.05


def wilson(k: int, n: int, alpha: float = 0.05) -> Accuracy:
    if n == 0:
        return Accuracy(0, 0, float("nan"), float("nan"), float("nan"), alpha)
    lo, hi = proportion_confint(k, n, alpha=alpha, method="wilson")
    return Accuracy(n, k, k / n, float(lo), float(hi), alpha)


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided p-value on discordant pairs. b = A right/B wrong, c = A wrong/B right."""
    if b + c == 0:
        return 1.0
    return float(binomtest(min(b, c), b + c, 0.5).pvalue)


@dataclass(frozen=True)
class PairedDiff:
    n: int
    b: int
    c: int
    diff: float          # acc_a - acc_b
    wald_lo: float
    wald_hi: float
    boot_lo: float
    boot_hi: float
    p_mcnemar: float
    verdict: str         # "A better", "B better", "tied"


def paired_diff(correct_a: list[bool], correct_b: list[bool], cluster_key: list[str],
                n_boot: int = 10_000, seed: int = 0, alpha: float = 0.05) -> PairedDiff:
    a = np.asarray(correct_a, dtype=float)
    bb = np.asarray(correct_b, dtype=float)
    assert a.shape == bb.shape == (len(cluster_key),)
    n = len(a)
    b = int(((a == 1) & (bb == 0)).sum())
    c = int(((a == 0) & (bb == 1)).sum())
    diff = float(a.mean() - bb.mean())

    # Wald paired SE for a difference of proportions on matched items.
    se = np.sqrt(max(b + c - (b - c) ** 2 / n, 0)) / n
    z = float(norm.ppf(1 - alpha / 2))
    wald_lo, wald_hi = diff - z * se, diff + z * se

    # Cluster bootstrap: resample clusters (videos) with replacement.
    keys = np.asarray(cluster_key)
    uniq, inv = np.unique(keys, return_inverse=True)
    per_cluster_d = np.zeros(len(uniq))
    per_cluster_n = np.zeros(len(uniq))
    np.add.at(per_cluster_d, inv, a - bb)
    np.add.at(per_cluster_n, inv, 1)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    boots = per_cluster_d[idx].sum(axis=1) / per_cluster_n[idx].sum(axis=1)
    boot_lo, boot_hi = (float(x) for x in np.quantile(boots, [alpha / 2, 1 - alpha / 2]))

    p = mcnemar(b, c)
    if p < alpha and (boot_lo > 0 or boot_hi < 0):
        verdict = "A better" if diff > 0 else "B better"
    else:
        verdict = "tied"
    return PairedDiff(n, b, c, diff, float(wald_lo), float(wald_hi), boot_lo, boot_hi, p, verdict)


def compare_runs(rows_a: list[dict], rows_b: list[dict]) -> dict:
    """Join two runs on item_id and compare. Rows are Scored dicts (see publish.load_run)."""
    by_b = {r["item_id"]: r for r in rows_b}
    common = [r for r in rows_a if r["item_id"] in by_b]
    if not common:
        return {"error": "no common items"}
    ca = [bool(r["correct"]) for r in common]
    cb = [bool(by_b[r["item_id"]]["correct"]) for r in common]
    keys = [r["cluster_key"] for r in common]
    acc_a = wilson(sum(ca), len(ca))
    acc_b = wilson(sum(cb), len(cb))
    d = paired_diff(ca, cb, keys)
    return {
        "model_a": common[0]["model_id"],
        "model_b": by_b[common[0]["item_id"]]["model_id"],
        "n_common": len(common),
        "acc_a": acc_a.__dict__,
        "acc_b": acc_b.__dict__,
        "paired": d.__dict__,
        "summary": (
            f"{common[0]['model_id']} {acc_a.acc:.1%} [{acc_a.lo:.1%}, {acc_a.hi:.1%}] vs "
            f"{by_b[common[0]['item_id']]['model_id']} {acc_b.acc:.1%} [{acc_b.lo:.1%}, {acc_b.hi:.1%}]; "
            f"diff {d.diff:+.1%} (bootstrap [{d.boot_lo:+.1%}, {d.boot_hi:+.1%}]), "
            f"discordant b={d.b} c={d.c}, McNemar p={d.p_mcnemar:.3f}: {d.verdict}"
        ),
    }
