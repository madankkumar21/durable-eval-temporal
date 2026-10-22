"""Load MVBench items and fetch only the videos we need.

Data: https://huggingface.co/datasets/OpenGVLab/MVBench (public, not gated).
Questions live in json/<task>.json. Videos live in large zips under video/.
We read single members out of the zip over HTTP range requests so a 100-item
run does not download 11 GB.
"""

from __future__ import annotations

import itertools
import json
import random
import string
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import DATA_DIR

HF_REPO = "OpenGVLab/MVBench"
HF_RESOLVE = f"https://huggingface.co/datasets/{HF_REPO}/resolve/main"

# task -> zip under video/. From lmms-eval's mvbench task map.
TASK_ZIP = {
    "action_count": "perception.zip",
    "action_sequence": "star.zip",
    "action_prediction": "star.zip",
    "object_interaction": "star.zip",
    "object_existence": "clevrer.zip",
    "moving_count": "clevrer.zip",
    "moving_attribute": "clevrer.zip",
    "counterfactual_inference": "clevrer.zip",
    "moving_direction": "clevrer.zip",
}

LETTERS = string.ascii_uppercase


def labels_for(style: str, n: int) -> list[str]:
    """Option labels as shown to the model. Results are always stored as letters by position."""
    if style == "letter":
        return list(LETTERS[:n])
    if style == "number":
        return [str(i + 1) for i in range(n)]
    raise ValueError(f"unknown label style {style!r}")


@dataclass(frozen=True)
class Item:
    item_id: str
    task: str
    video_name: str
    question: str
    candidates: tuple[str, ...]
    answer: str
    cluster_key: str  # items that share a video share a key; action_count is one question per video

    @property
    def answer_letter(self) -> str:
        return LETTERS[self.candidates.index(self.answer)]

    def options_text(self, style: str = "letter") -> str:
        labels = labels_for(style, len(self.candidates))
        return "\n".join(f"({lab}) {c}" for lab, c in zip(labels, self.candidates))


def _questions_path(task: str, data_dir: Path) -> Path:
    path = data_dir / "json" / f"{task}.json"
    if not path.exists():
        from huggingface_hub import hf_hub_download

        hf_hub_download(
            repo_id=HF_REPO,
            repo_type="dataset",
            filename=f"json/{task}.json",
            local_dir=data_dir,
        )
    return path


def permutation_order(index: int, n_options: int) -> tuple[int, ...]:
    """The index-th ordering of n options, in itertools.permutations order. Index 0 is the identity.

    Entry j is the original position of the option shown in slot j. Over all n! indices, every
    option sits in every slot equally often, which is what permutation-averaged metrics rely on.
    """
    orders = list(itertools.permutations(range(n_options)))
    if not 0 <= index < len(orders):
        raise ValueError(f"permutation index {index} out of range 0..{len(orders) - 1} for {n_options} options")
    return orders[index]


def _value_shuffle(candidates: list[str], avoid_middle: str | None, seed: int, item_id: str) -> list[str]:
    """Seeded per-item reorder that keeps `avoid_middle` out of the middle slot when the item has it.

    With the slice's majority answer value kept out of the middle, "picked the middle slot" and
    "picked the majority value" are disjoint on every item, which separates a positional prior
    from a value prior. Own RNG namespace, so it never coincides with a shuffle_seed order.
    """
    rng = random.Random(f"values:{seed}:{item_id}")
    n = len(candidates)
    orders = list(itertools.permutations(range(n)))
    if n % 2 == 1 and avoid_middle in candidates:
        mid = n // 2
        orders = [o for o in orders if candidates[o[mid]] != avoid_middle]
    order = rng.choice(orders)
    return [candidates[j] for j in order]


def load_items(task: str = "action_count", n: int = 100, data_dir: Path = DATA_DIR,
               shuffle_seed: int | None = None, permutation: int | None = None,
               value_shuffle_seed: int | None = None, start: int = 0) -> list[Item]:
    """n items in file order starting at `start`. Deterministic, so every engine sees the same slice.

    At most one option-order knob may be set; item_id never changes, so any two runs pair up:
    - shuffle_seed: permute each item's options with a per-item RNG seeded by (seed, item_id).
    - permutation: apply the same fixed ordering (0..n!-1, 0 = identity) to every item.
    - value_shuffle_seed: per-item seeded order that keeps the slice's majority answer value
      out of the middle slot (see _value_shuffle).
    """
    knobs = [k for k, v in (("shuffle_seed", shuffle_seed), ("permutation", permutation),
                            ("value_shuffle_seed", value_shuffle_seed)) if v is not None]
    if len(knobs) > 1:
        raise ValueError(f"set at most one of shuffle_seed, permutation, value_shuffle_seed (got {knobs})")
    raw = json.loads(_questions_path(task, data_dir).read_text())
    rows = raw[start:start + n]
    majority = Counter(r["answer"] for r in rows).most_common(1)[0][0] if rows else None
    items = []
    for row in rows:
        video = row["video"]
        item_id = f"{task}:{Path(video).stem}"
        candidates = list(row["candidates"])
        if shuffle_seed is not None:
            random.Random(f"{shuffle_seed}:{item_id}").shuffle(candidates)
        elif permutation is not None:
            order = permutation_order(permutation, len(candidates))
            candidates = [candidates[j] for j in order]
        elif value_shuffle_seed is not None:
            candidates = _value_shuffle(candidates, majority, value_shuffle_seed, item_id)
        items.append(
            Item(
                item_id=item_id,
                task=task,
                video_name=video,
                question=row["question"],
                candidates=tuple(candidates),
                answer=row["answer"],
                cluster_key=Path(video).stem,
            )
        )
    return items


def video_path(item: Item, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "videos" / item.task / item.video_name


def ensure_videos(items: list[Item], data_dir: Path = DATA_DIR) -> list[Path]:
    """Extract any missing videos from the task's zip on Hugging Face, by range request.

    Opens the remote zip once per task and pulls only the members we need.
    Falls back to a full download of the zip if range requests are refused.
    """
    missing = [it for it in items if not video_path(it, data_dir).exists()]
    if not missing:
        return [video_path(it, data_dir) for it in items]

    by_task: dict[str, list[Item]] = {}
    for it in missing:
        by_task.setdefault(it.task, []).append(it)

    for task, group in by_task.items():
        zip_name = TASK_ZIP[task]
        out_dir = data_dir / "videos" / task
        out_dir.mkdir(parents=True, exist_ok=True)
        _extract_members(zip_name, group, out_dir, data_dir)

    return [video_path(it, data_dir) for it in items]


def _extract_members(zip_name: str, group: list[Item], out_dir: Path, data_dir: Path) -> None:
    import zipfile

    url = f"{HF_RESOLVE}/video/{zip_name}"
    local_zip = data_dir / "zips" / zip_name

    if local_zip.exists():
        zf = zipfile.ZipFile(local_zip)
    else:
        from remotezip import RemoteZip

        zf = RemoteZip(url)

    with zf:
        names = zf.namelist()
        by_base = {Path(n).name: n for n in names if not n.endswith("/")}
        for it in group:
            member = by_base.get(it.video_name)
            if member is None:
                raise FileNotFoundError(f"{it.video_name} not in {zip_name}; first members: {names[:5]}")
            target = out_dir / it.video_name
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
