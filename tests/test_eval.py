"""Eval harness: runs (instruction, source, expected_plan/frames)
tuples through the planner and checks. Frame-level checks require
ffmpeg + a real source; the plan-equality path runs offline."""

from __future__ import annotations

from videoagent.eval import EvalCase, run_suite
from videoagent.ops import Cut, FadeIn, Plan
from videoagent.planner import FakeChatClient, fake_tool_calls
from videoagent.verifier import SourceProbe


def _src(d: float = 60.0) -> SourceProbe:
    return SourceProbe(duration_s=d, width=1920, height=1080, fps=30.0)


def test_plan_equality_passes():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    case = EvalCase(
        name="cut_first_10",
        instruction="cut the first 10 seconds",
        source=_src(60),
        expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)]),
    )
    rep = run_suite([case], chat)
    assert rep.passed == 1
    assert rep.cases[0].ok is True


def test_plan_equality_with_float_tolerance():
    """The LLM can emit 10 vs 10.0 vs 10.001; the harness tolerates
    sub-millisecond diffs."""
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0001})),
    ])
    case = EvalCase(
        name="cut_first_10_close_enough",
        instruction="cut the first 10 seconds",
        source=_src(60),
        expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)]),
    )
    assert run_suite([case], chat).passed == 1


def test_plan_inequality_fails_with_diff():
    """Critical: this is the failure mode the eval is designed to
    catch. The model emits a plausible-looking but WRONG plan; the
    harness flags it."""
    chat = FakeChatClient(responses=[
        # Wrong: cut 0..15 instead of 0..10.
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 15.0})),
    ])
    case = EvalCase(
        name="cut_first_10_but_wrong",
        instruction="cut the first 10 seconds",
        source=_src(60),
        expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)]),
    )
    rep = run_suite([case], chat)
    assert rep.passed == 0
    assert "plan mismatch" in rep.cases[0].error


def test_unverifiable_plan_marked_failed():
    """When the planner exhausts retries without a verifying plan,
    the case is failed with the error chain."""
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
    ])
    case = EvalCase(
        name="impossible",
        instruction="cut everything",
        source=_src(60),
        expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=60.0)]),
    )
    rep = run_suite([case], chat)
    assert rep.passed == 0
    assert "cut_end_past_source" in rep.cases[0].error


def test_multi_case_suite_aggregates():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
        fake_tool_calls(("fade_in", {"at_s": 0.0, "duration_s": 1.0})),
    ])
    cases = [
        EvalCase(name="c1", instruction="cut first 10s",
                 source=_src(60),
                 expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)])),
        EvalCase(name="c2", instruction="fade in",
                 source=_src(60),
                 expected_plan=Plan(ops=[FadeIn(at_s=0.0, duration_s=1.0)])),
    ]
    rep = run_suite(cases, chat)
    assert rep.passed == 2
    assert rep.total == 2


def test_report_to_json_round_trip():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    case = EvalCase(name="x", instruction="cut", source=_src(60),
                    expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)]))
    rep = run_suite([case], chat)
    js = rep.to_json()
    assert js["passed"] == 1 and js["total"] == 1
    assert js["cases"][0]["plan"]["ops"][0]["op"] == "cut"
