"""Smoke test for the 63-trial harness — verifies category counts, that
totals add to 63, and that the autonomous/escalated split is 44/19."""
from __future__ import annotations

from ic.live_trials import (
    run_confirmed, run_exclusion, run_reversible,
    run_insufficient, run_irreversible, summarize,
)


def test_63_trials_total_and_split():
    trials = (run_confirmed() + run_exclusion() + run_reversible()
              + run_insufficient() + run_irreversible())
    assert len(trials) == 63

    s = summarize(trials)
    assert s["autonomous"] == 44
    assert s["escalated"] == 19
    assert s["total"] == 63


def test_each_category_hits_expected_counts():
    assert len(run_confirmed())    == 25
    assert len(run_exclusion())    == 10
    assert len(run_reversible())   == 9
    assert len(run_insufficient()) == 5
    assert len(run_irreversible()) == 14


def test_confirmed_category_all_succeed():
    trials = run_confirmed()
    assert all(t.outcome == "success" for t in trials), \
        [t for t in trials if t.outcome != "success"]


def test_irreversible_all_denied():
    trials = run_irreversible()
    assert all(t.outcome == "denied" for t in trials), \
        [t for t in trials if t.outcome != "denied"]


def test_insufficient_all_escalate():
    trials = run_insufficient()
    assert all(t.outcome == "escalated" for t in trials)
