"""Option-order knobs on load_items: fixed permutations, value-shuffle, start offset."""

import json
from collections import Counter

import pytest

from core.tasks import load_items, permutation_order

ANSWERS = ["3", "3", "2", "3", "5", "4", "3", "2"]


@pytest.fixture
def data_dir(tmp_path):
    rows = []
    for i, ans in enumerate(ANSWERS):
        cands = sorted({ans, str(int(ans) + 1), str(max(int(ans) - 1, 1))}, key=int)
        if len(cands) < 3:
            cands = [ans, str(int(ans) + 1), str(int(ans) + 2)]
        rows.append({"video": f"video_{i}.mp4", "question": "How many?", "candidates": cands, "answer": ans})
    (tmp_path / "json").mkdir()
    (tmp_path / "json" / "action_count.json").write_text(json.dumps(rows))
    return tmp_path


def test_permutation_zero_is_identity(data_dir):
    base = load_items("action_count", 8, data_dir)
    p0 = load_items("action_count", 8, data_dir, permutation=0)
    assert [it.candidates for it in base] == [it.candidates for it in p0]


def test_six_orderings_are_distinct_and_balanced(data_dir):
    orders = [permutation_order(k, 3) for k in range(6)]
    assert len(set(orders)) == 6
    # every original option sits in every slot exactly twice over the 6 orderings
    for slot in range(3):
        assert Counter(o[slot] for o in orders) == {0: 2, 1: 2, 2: 2}
    base = load_items("action_count", 8, data_dir)
    for k in range(6):
        items = load_items("action_count", 8, data_dir, permutation=k)
        for b, it in zip(base, items):
            assert it.item_id == b.item_id and it.answer == b.answer
            assert it.candidates == tuple(b.candidates[j] for j in orders[k])
            assert it.candidates[it.candidates.index(it.answer)] == it.answer


def test_permutation_out_of_range(data_dir):
    with pytest.raises(ValueError):
        load_items("action_count", 8, data_dir, permutation=6)


def test_knobs_are_mutually_exclusive(data_dir):
    with pytest.raises(ValueError):
        load_items("action_count", 8, data_dir, shuffle_seed=1, permutation=2)
    with pytest.raises(ValueError):
        load_items("action_count", 8, data_dir, permutation=1, value_shuffle_seed=1)


def test_value_shuffle_keeps_majority_value_off_the_middle(data_dir):
    items = load_items("action_count", 8, data_dir, value_shuffle_seed=1)
    majority = Counter(ANSWERS).most_common(1)[0][0]
    assert majority == "3"
    for it in items:
        if majority in it.candidates:
            assert it.candidates[1] != majority
    again = load_items("action_count", 8, data_dir, value_shuffle_seed=1)
    assert [it.candidates for it in items] == [it.candidates for it in again]  # deterministic
    base = load_items("action_count", 8, data_dir)
    assert [sorted(it.candidates) for it in items] == [sorted(b.candidates) for b in base]  # same options


def test_value_shuffle_differs_from_shuffle_seed(data_dir):
    v = [it.candidates for it in load_items("action_count", 8, data_dir, value_shuffle_seed=1)]
    s = [it.candidates for it in load_items("action_count", 8, data_dir, shuffle_seed=1)]
    assert v != s


def test_start_offset(data_dir):
    tail = load_items("action_count", 3, data_dir, start=5)
    assert [it.item_id for it in tail] == ["action_count:video_5", "action_count:video_6", "action_count:video_7"]
