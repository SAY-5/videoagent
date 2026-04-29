"""Verifier catches the source-dependent failures Pydantic can't.
This is the v1 'tight schemas' thesis: the model's tool-call output
parses, but the timecodes don't fit THIS source."""

from __future__ import annotations

from videoagent.ops import (
    Cut,
    FadeOut,
    Plan,
    Speed,
    Trim,
    Volume,
)
from videoagent.verifier import SourceProbe, errors_for_replan, verify


def _src(duration: float = 120.0, has_audio: bool = True) -> SourceProbe:
    return SourceProbe(duration_s=duration, has_audio=has_audio,
                       width=1920, height=1080, fps=30.0)


def test_cut_within_source_passes():
    p = Plan(ops=[Cut(start_s=10.0, end_s=20.0)])
    assert verify(p, _src(120)) == []


def test_cut_past_source_caught():
    p = Plan(ops=[Cut(start_s=10.0, end_s=200.0)])
    errs = verify(p, _src(120))
    assert len(errs) == 1
    assert errs[0].code == "cut_end_past_source"
    assert "200.000" in errs[0].hint and "120.000" in errs[0].hint


def test_cut_start_past_source_caught():
    p = Plan(ops=[Cut(start_s=200.0, end_s=205.0)])
    errs = verify(p, _src(120))
    assert any(e.code == "cut_start_past_source" for e in errs)


def test_trim_past_source_caught():
    p = Plan(ops=[Trim(keep_start_s=0.0, keep_end_s=200.0)])
    errs = verify(p, _src(120))
    assert errs and errs[0].code == "trim_end_past_source"


def test_trim_too_short_rejected():
    p = Plan(ops=[Trim(keep_start_s=10.0, keep_end_s=10.05)])
    errs = verify(p, _src(120))
    assert any(e.code == "trim_too_short" for e in errs)


def test_fade_past_source_caught():
    p = Plan(ops=[FadeOut(at_s=119.0, duration_s=5.0)])
    errs = verify(p, _src(120))
    assert errs and errs[0].code == "fade_past_source"


def test_volume_without_audio_caught():
    p = Plan(ops=[Volume(factor=2.0)])
    errs = verify(p, _src(120, has_audio=False))
    assert errs and errs[0].code == "volume_no_audio"


def test_volume_with_audio_passes():
    p = Plan(ops=[Volume(factor=2.0)])
    assert verify(p, _src(120, has_audio=True)) == []


def test_speed_yielding_too_long_output_caught():
    """A 1-day source @ 0.001x = 1000 days; refuse."""
    p = Plan(ops=[Speed(factor=0.0001)])
    errs = verify(p, _src(86400))
    assert any(e.code == "speed_too_long" for e in errs)


def test_errors_for_replan_is_human_readable():
    p = Plan(ops=[
        Cut(start_s=0.0, end_s=200.0),
        FadeOut(at_s=119.0, duration_s=5.0),
    ])
    errs = verify(p, _src(120))
    msg = errors_for_replan(errs)
    assert "cut_end_past_source" in msg
    assert "fade_past_source" in msg
    assert "Fix these specific problems" in msg
