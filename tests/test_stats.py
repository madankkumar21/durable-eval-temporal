import math

from core.stats import mcnemar, paired_diff, wilson


def test_wilson_textbook():
    a = wilson(85, 100)
    assert math.isclose(a.acc, 0.85)
    # Wilson 95% for 85/100 is about [0.767, 0.908]
    assert abs(a.lo - 0.767) < 0.005
    assert abs(a.hi - 0.908) < 0.005


def test_wilson_edges():
    assert wilson(0, 100).lo == 0.0
    assert wilson(100, 100).hi == 1.0
    assert math.isnan(wilson(0, 0).acc)


def test_mcnemar_exact():
    # 15 vs 5 discordant: exact two-sided binomial p about 0.041
    assert abs(mcnemar(15, 5) - 0.0414) < 0.002
    assert mcnemar(0, 0) == 1.0
    assert mcnemar(10, 10) == 1.0


def test_paired_diff_direction_and_tie():
    n = 100
    # A right on all; B wrong on first 20 -> A better
    a = [True] * n
    b = [False] * 20 + [True] * 80
    keys = [str(i) for i in range(n)]
    d = paired_diff(a, b, keys, n_boot=2000)
    assert d.b == 20 and d.c == 0
    assert abs(d.diff - 0.20) < 1e-9
    assert d.verdict == "A better"
    assert d.boot_lo > 0

    # symmetric discordance -> tied
    a2 = [True] * 10 + [False] * 10 + [True] * 80
    b2 = [False] * 10 + [True] * 10 + [True] * 80
    d2 = paired_diff(a2, b2, keys, n_boot=2000)
    assert d2.verdict == "tied"
    assert d2.b == 10 and d2.c == 10


def test_cluster_bootstrap_widens_with_clusters():
    # Same item-level data, but grouped into 10 clusters where disagreement is concentrated:
    # the clustered interval must be at least as wide as the item-level one.
    n = 100
    a = [True] * n
    b = [i % 10 != 0 for i in range(n)]  # 10 discordant items
    item_keys = [str(i) for i in range(n)]
    cluster_keys = [str(i % 10) for i in range(n)]  # discordant items all in cluster "0"
    d_item = paired_diff(a, b, item_keys, n_boot=4000)
    d_clu = paired_diff(a, b, cluster_keys, n_boot=4000)
    assert (d_clu.boot_hi - d_clu.boot_lo) >= (d_item.boot_hi - d_item.boot_lo) * 0.95
