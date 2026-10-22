"""Dataclasses that cross the workflow/activity boundary. Plain data only; no frames, no numpy."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Scenario 4: which workflow code this worker runs, read from the environment at import. Restarting
# the worker with a new value acts like a deploy.
#   v1            no progress step
#   v2            progress step behind workflow.patched("progress-v2")  (the safe change)
#   v2-unpatched  progress step with no patch                           (the unsafe change)
WORKFLOW_VERSION = os.environ.get("VEU_WORKFLOW_VERSION", "v1")


@dataclass
class EvalParams:
    run_id: str
    model_id: str = "stub"
    task: str = "action_count"
    n: int = 100
    n_frames: int = 8
    shuffle_seed: int | None = None
    label_style: str = "letter"
    chunk: int = 200              # continue-as-new after this many items (scenario 5)
    concurrency: int = 1          # items in flight at once (scenario 6)
    heartbeat_s: int = 90         # a model call silent this long is timed out and retried (scenario 7)
    start_index: int = 0          # carried across continue-as-new
    rows: list[dict] = field(default_factory=list)  # Scored rows so far, carried across continue-as-new


@dataclass
class ItemCall:
    run_id: str
    model_id: str
    task: str
    index: int                    # position in load_items(task) order; ids are re-derived in the activity
    n_frames: int
    shuffle_seed: int | None = None
    label_style: str = "letter"


@dataclass
class RunSummary:
    model_id: str
    n: int
    correct: int
    accuracy: float
    wilson_lo: float
    wilson_hi: float
    cost_usd: float
    cached_items: int
    parse_failures: int


@dataclass
class Progress:
    run_id: str
    done: int
    n: int
