"""Scenario 3: the approval update refuses a decision that is early, repeated or malformed.

The validator only reads workflow state and raises, so it runs here without a server or a worker.
The server-side checks (an early approval refused live, an approval after the run finished) are in
scripts/temporal_scenarios.sh s3.
"""

from __future__ import annotations

import pytest

from engines.temporal_app.shared import EvalParams
from engines.temporal_app.workflows import EvalWorkflow


def _workflow(scored: bool, decision: str | None = None) -> EvalWorkflow:
    wf = EvalWorkflow()
    wf.params = EvalParams(run_id="t", n=10)
    wf.progress = 10 if scored else 4
    wf.summary = object() if scored else None  # the validator only checks that a summary exists
    wf.decision = decision
    return wf


def test_refuses_approval_while_scoring():
    with pytest.raises(ValueError, match="still scoring: 4 of 10 items"):
        _workflow(scored=False).validate_decide("yes")


def test_refuses_a_value_other_than_yes_or_no():
    with pytest.raises(ValueError, match="yes or no"):
        _workflow(scored=True).validate_decide("yse")


def test_refuses_a_second_decision():
    with pytest.raises(ValueError, match="already decided: no"):
        _workflow(scored=True, decision="no").validate_decide("yes")


def test_accepts_a_decision_once_scored():
    _workflow(scored=True).validate_decide("yes")  # no exception


def test_decide_records_the_decision_and_answers():
    wf = _workflow(scored=True)
    assert wf.decide("yes") == "accepted: yes"
    assert wf.decision == "yes"
