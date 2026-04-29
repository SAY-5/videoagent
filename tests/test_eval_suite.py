"""v3 eval suite — runs the curated set with the deterministic CI
chat fixture and asserts every case passes. This is the regression
gate: any change that breaks the suite fails CI."""

from __future__ import annotations

from eval.cases import BOUNDED, EXACT, FAILURES, summary
from eval.runner import _ci_chat, run


def test_suite_summary_counts():
    s = summary()
    assert s["total"] == len(EXACT) + len(BOUNDED) + len(FAILURES)
    assert s["total"] >= 12, f"v3 promised ≥12 cases, got {s['total']}"


def test_suite_runs_green_with_ci_fixture():
    rep = run(_ci_chat())
    failed = [o for o in rep.outcomes if not o.ok]
    assert not failed, "\n".join(
        f"{o.kind}/{o.name}: {o.error}" for o in failed
    )
    assert rep.passed == rep.total


def test_failure_cases_assert_rejection_path():
    """The planner must REJECT the impossible-against-source cases.
    Without this, a permissive verifier could accidentally let
    cut-past-source through and we'd never notice."""
    rep = run(_ci_chat())
    failure_outcomes = [o for o in rep.outcomes if o.kind == "failure"]
    assert failure_outcomes, "no failure cases in the suite"
    assert all(o.ok for o in failure_outcomes), (
        "failure cases must surface their expected error code: "
        + "; ".join(f"{o.name}: {o.error}" for o in failure_outcomes if not o.ok)
    )


def test_bounded_predicate_catches_underspecified_plans():
    """Bounded cases use predicates because multiple plans can be
    correct. The predicate is what catches 'cut for 10 seconds AND
    fade at 1:30' producing only one of the two."""
    rep = run(_ci_chat())
    bounded = [o for o in rep.outcomes if o.kind == "bounded"]
    assert bounded
    for o in bounded:
        assert o.ok, f"{o.name}: {o.error}"
