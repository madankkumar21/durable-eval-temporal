"""The baseline: a plain loop. Loses in-flight progress on a crash; the core cache is its only resume.

Usage:
  uv run python -m engines.plain --model stub --n 10
  uv run python -m engines.plain --model claude-haiku-4-5-20251001 --n 100
  uv run python -m engines.plain --model stub-b --n 100 --compare-with stub
"""

from __future__ import annotations

import argparse
import sys
import time

from core.frames import FrameCfg
from core.model import RetryableModelError, answer_item, http_calls_since
from core.publish import latest_run_for_model, load_run, publish
from core.score import score
from core.stats import compare_runs
from core.tasks import load_items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="stub")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--task", default="action_count")
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--shuffle-seed", type=int, default=None, help="permute each item's options with this seed")
    ap.add_argument("--permutation", type=int, default=None, help="apply option ordering 0..5 to every item (0 = original order)")
    ap.add_argument("--value-shuffle-seed", type=int, default=None,
                    help="per-item seeded order that keeps the majority answer value out of the middle slot")
    ap.add_argument("--start", type=int, default=0, help="skip this many items in file order (e.g. 100 for items 101-200)")
    ap.add_argument("--blind", action="store_true", help="send no frames: question and options only")
    ap.add_argument("--labels", default="letter", choices=["letter", "number"], help="how options are labelled in the prompt")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--compare-with", default=None, help="model_id of a published run to compare against")
    ap.add_argument("--yes", action="store_true", help="skip the human approval prompt")
    ap.add_argument("--max-retries", type=int, default=5)
    args = ap.parse_args(argv)

    shuf = f"-shuf{args.shuffle_seed}" if args.shuffle_seed is not None else ""
    run_id = args.run_id or f"{args.model}-{args.task}-n{args.n}{shuf}-{time.strftime('%Y%m%d-%H%M%S')}"
    cfg = FrameCfg(n_frames=args.frames, label_style=args.labels, blind=args.blind)
    t_start = time.time()

    items = load_items(args.task, args.n, shuffle_seed=args.shuffle_seed, permutation=args.permutation,
                       value_shuffle_seed=args.value_shuffle_seed, start=args.start)
    rows = []
    for i, item in enumerate(items, 1):
        attempt = 0
        while True:
            try:
                result = answer_item(item, args.model, cfg, run_id)
                break
            except RetryableModelError as e:
                attempt += 1
                if attempt > args.max_retries:
                    raise
                delay = min(2 ** attempt, 60)
                print(f"[{i}/{len(items)}] retryable error ({e}); retry {attempt} in {delay}s", file=sys.stderr)
                time.sleep(delay)
        row = score(item, result)
        rows.append(row)
        flag = "cache" if row.cached else f"${row.cost_usd:.4f}"
        print(f"[{i}/{len(items)}] {item.item_id} pred={row.pred_letter} ans={row.answer_letter} "
              f"{'ok ' if row.correct else 'MISS'} {flag}")

    n_ok = sum(r.correct for r in rows)
    print(f"\n{args.model}: {n_ok}/{len(rows)} correct; cost ${sum(r.cost_usd for r in rows):.4f}; "
          f"API calls this run: {http_calls_since(t_start, args.model)}")

    comparison = None
    if args.compare_with:
        other = latest_run_for_model(args.compare_with)
        if other is None:
            print(f"no published run found for {args.compare_with}", file=sys.stderr)
        else:
            rows_a = [r.__dict__ for r in rows]
            comparison = compare_runs(rows_a, load_run(other))
            print(comparison["summary"])

    if not args.yes:
        ans = input("\nPublish these results? [y/N] ").strip().lower()
        if ans != "y":
            print("not published")
            return 1
    extra = {"shuffle_seed": args.shuffle_seed, "label_style": args.labels,
             "permutation": args.permutation, "value_shuffle_seed": args.value_shuffle_seed,
             "start": args.start, "blind": args.blind, "n_frames": args.frames}
    if comparison:
        extra["comparison"] = comparison
    out = publish(run_id, rows, extra=extra)
    print(f"published {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
