"""Curated eval suite — the v3 thesis.

Twelve cases that exercise each op + the cross-op interactions where
the LLM has historically hallucinated. The harness runs them on every
PR (CI), and any regression fails the build.

Cases come in three flavors:
  exact    : expected_plan must match (with float tolerance).
  bounded  : the plan must satisfy a custom predicate (e.g. 'cuts the
             first ≤10s but not more') — useful when several plans
             are equally correct.
  failure  : the planner is expected to FAIL because the instruction
             is impossible against the given source. v3 explicitly
             tests the rejection path so we don't accidentally make
             the planner too permissive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from videoagent.eval import EvalCase
from videoagent.ops import (
    Concat,
    Cut,
    FadeIn,
    FadeOut,
    Plan,
    Resize,
    Speed,
    Trim,
    Volume,
)
from videoagent.verifier import SourceProbe


@dataclass
class BoundedCase:
    """A case where exact plan equality is too strict; we assert a
    predicate over the resulting Plan instead."""

    name: str
    instruction: str
    source: SourceProbe
    predicate: Callable[[Plan], bool]
    explanation: str


@dataclass
class FailureCase:
    """A case where the planner should FAIL because the instruction
    is impossible against the given source. Tests the rejection path."""

    name: str
    instruction: str
    source: SourceProbe
    expected_error_code: str


# Reused source probes.
def _src(d: float = 60.0, has_audio: bool = True) -> SourceProbe:
    return SourceProbe(duration_s=d, has_audio=has_audio,
                       width=1920, height=1080, fps=30.0)


# --- exact cases ---------------------------------------------------

EXACT: list[EvalCase] = [
    EvalCase(
        name="cut_first_10s",
        instruction="cut the first 10 seconds",
        source=_src(60),
        expected_plan=Plan(ops=[Cut(start_s=0.0, end_s=10.0)]),
    ),
    EvalCase(
        name="fade_in_at_zero",
        instruction="fade in at the start over 1 second",
        source=_src(60),
        expected_plan=Plan(ops=[FadeIn(at_s=0.0, duration_s=1.0)]),
    ),
    EvalCase(
        name="fade_out_last_2s",
        instruction="fade out at 1:30 over 2 seconds",
        source=_src(120),
        expected_plan=Plan(ops=[FadeOut(at_s=90.0, duration_s=2.0)]),
    ),
    EvalCase(
        name="resize_to_720p",
        instruction="resize to 1280x720",
        source=_src(60),
        expected_plan=Plan(ops=[Resize(width=1280, height=720)]),
    ),
    EvalCase(
        name="speed_2x",
        instruction="play at 2x speed",
        source=_src(60),
        expected_plan=Plan(ops=[Speed(factor=2.0)]),
    ),
    EvalCase(
        name="volume_half",
        instruction="halve the volume",
        source=_src(60),
        expected_plan=Plan(ops=[Volume(factor=0.5)]),
    ),
    EvalCase(
        name="trim_middle_30s",
        instruction="keep just from 5 to 35 seconds",
        source=_src(60),
        expected_plan=Plan(ops=[Trim(keep_start_s=5.0, keep_end_s=35.0)]),
    ),
]


# --- bounded cases (multiple correct plans) -------------------------

BOUNDED: list[BoundedCase] = [
    BoundedCase(
        name="cut_first_10_and_fade_at_90s",
        instruction="cut the first 10 seconds and fade out at 1:30",
        source=_src(120),
        predicate=lambda p: (
            len(p.ops) == 2
            and isinstance(p.ops[0], Cut) and p.ops[0].start_s == 0.0 and abs(p.ops[0].end_s - 10.0) < 0.5
            and isinstance(p.ops[1], FadeOut) and abs(p.ops[1].at_s - 90.0) < 1.0
        ),
        explanation="Multi-op: must include a Cut(0, ~10) AND a FadeOut at ~90s.",
    ),
    BoundedCase(
        name="concat_two_clips",
        instruction="concatenate with bonus.mp4",
        source=_src(60),
        predicate=lambda p: (
            len(p.ops) == 1
            and isinstance(p.ops[0], Concat)
            and any("bonus" in i for i in p.ops[0].inputs)
        ),
        explanation="Concat with at least one input that mentions 'bonus'.",
    ),
]


# --- failure cases (must reject) ------------------------------------

FAILURES: list[FailureCase] = [
    FailureCase(
        name="cut_past_source",
        instruction="cut from 30 seconds to 5 minutes",
        source=_src(60),
        expected_error_code="cut_end_past_source",
    ),
    FailureCase(
        name="volume_no_audio",
        instruction="halve the volume",
        source=_src(60, has_audio=False),
        expected_error_code="volume_no_audio",
    ),
    FailureCase(
        name="fade_past_source",
        instruction="fade out starting at 58 seconds for 5 seconds",
        source=_src(60),
        expected_error_code="fade_past_source",
    ),
]


# --- summary ---------------------------------------------------------

def summary() -> dict[str, Any]:
    return {
        "exact":   [c.name for c in EXACT],
        "bounded": [c.name for c in BOUNDED],
        "failure": [c.name for c in FAILURES],
        "total":   len(EXACT) + len(BOUNDED) + len(FAILURES),
    }
