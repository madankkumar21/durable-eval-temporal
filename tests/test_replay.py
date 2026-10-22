"""Scenario 4: can new workflow code safely replay a run that started on the old code?

tests/histories/v1_run.json is a real 5-item stub run recorded on the v1 code (no progress step),
exported with `temporal workflow show -w replay-v1 --output json`. The Replayer re-runs the
workflow code against that history with no server and fails if the code would make different
decisions. This is the check to run in CI before a deploy.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from engines.temporal_app.workflows import EvalWorkflow

HISTORY = Path(__file__).parent / "histories" / "v1_run.json"


def _replay(version: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The workflow sandbox re-imports shared.py for each run, so the version comes from the
    # environment, exactly as it does for a worker process.
    monkeypatch.setenv("VEU_WORKFLOW_VERSION", version)
    history = WorkflowHistory.from_json("replay-v1", HISTORY.read_text())
    asyncio.run(Replayer(workflows=[EvalWorkflow]).replay_workflow(history))


def test_v1_code_replays_v1_history(monkeypatch):
    _replay("v1", monkeypatch)


def test_patched_v2_replays_v1_history(monkeypatch):
    # workflow.patched() returns False on history written before the change, so the old path is taken.
    _replay("v2", monkeypatch)


def test_unpatched_v2_breaks_v1_history(monkeypatch):
    # The same change without patched(): the code schedules an activity the history never recorded.
    with pytest.raises(Exception, match="(?i)nondetermin"):
        _replay("v2-unpatched", monkeypatch)
