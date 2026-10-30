"""Scenario 4: can new workflow code safely replay a run that started on the old code?

tests/histories/v1_run.json is a real 5-item stub run recorded on the v1 code (no progress step),
exported with `temporal workflow show -w replay-v1 --output json`. The Replayer re-runs the
workflow code against that history with no server and fails if the code would make different
decisions. This is the check to run in CI before a deploy.

tests/histories/approval_expired.json is scenario 3b: a 5-item run with a 20 s approval deadline,
the worker killed during the wait, and the timer fired with no worker running. Host names are
replaced with "dev-machine".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from engines.temporal_app.workflows import EvalWorkflow

HISTORIES = Path(__file__).parent / "histories"


def _replay(version: str, monkeypatch: pytest.MonkeyPatch, history_file: str = "v1_run.json") -> None:
    # The workflow sandbox re-imports shared.py for each run, so the version comes from the
    # environment, exactly as it does for a worker process.
    monkeypatch.setenv("VEU_WORKFLOW_VERSION", version)
    history = WorkflowHistory.from_json(f"replay-{history_file}", (HISTORIES / history_file).read_text())
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


def test_approval_deadline_replays(monkeypatch):
    # Scenario 3b: the deadline's timer replays on the current code. The Replayer compares commands,
    # not the returned value, so this guards the timer, not the "expired" result. v1 above has no
    # deadline and records no timer, and still replays: the deadline only starts a timer when set.
    _replay("v1", monkeypatch, "approval_expired.json")
