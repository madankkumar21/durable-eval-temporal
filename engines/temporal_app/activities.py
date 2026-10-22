"""Activities wrap core functions. They run outside the workflow sandbox, so they may import anything."""

from __future__ import annotations

import json
from dataclasses import asdict

from temporalio import activity
from temporalio.exceptions import ApplicationError

from core.frames import FrameCfg
from core.model import FatalModelError, RetryableModelError, answer_item
from core import RESULTS_DIR
from core.publish import publish, run_summary
from core.score import Scored, score
from core.tasks import load_items

from .shared import ItemCall, Progress, RunSummary


@activity.defn
def answer_item_activity(call: ItemCall) -> dict:
    """One model call. Retryable errors propagate (Temporal's RetryPolicy handles backoff);
    fatal errors are marked non-retryable so the workflow fails fast."""
    item = load_items(call.task, call.index + 1, shuffle_seed=call.shuffle_seed)[call.index]
    activity.heartbeat(item.item_id)
    print(f"[{call.run_id}] item {call.index + 1} {item.item_id} attempt {activity.info().attempt}", flush=True)
    try:
        result = answer_item(item, call.model_id, FrameCfg(n_frames=call.n_frames, label_style=call.label_style), call.run_id)
    except RetryableModelError as e:
        raise ApplicationError(str(e), type="Retryable") from e
    except FatalModelError as e:
        raise ApplicationError(str(e), type="Fatal", non_retryable=True) from e
    return asdict(score(item, result))


@activity.defn
def summarize_activity(rows: list[dict]) -> RunSummary:
    s = run_summary([Scored(**r) for r in rows])
    return RunSummary(
        model_id=s["model_id"], n=s["n"], correct=s["correct"], accuracy=s["accuracy"],
        wilson_lo=s["wilson_lo"], wilson_hi=s["wilson_hi"], cost_usd=s["cost_usd"],
        cached_items=s["cached_items"], parse_failures=s["parse_failures"],
    )


@activity.defn
def publish_activity(run_id: str, rows: list[dict]) -> str:
    return str(publish(run_id, [Scored(**r) for r in rows], extra={"engine": "temporal"}))


@activity.defn
def progress_activity(p: Progress) -> None:
    """Scenario 4: the v2 step. Writes a progress file a dashboard could read."""
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"{p.run_id}.progress.json").write_text(json.dumps(p.__dict__))
