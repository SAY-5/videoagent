"""Argv builder. Mirrors the Go side's BuildArgv tests so any
divergence between languages shows up immediately."""

from __future__ import annotations

from videoagent.ffmpeg_run import build_argv
from videoagent.ops import (
    Cut,
    FadeIn,
    FadeOut,
    Plan,
    Resize,
    Speed,
    Trim,
    Volume,
)


def _argv(plan: Plan) -> list[str]:
    return build_argv("in.mp4", plan, "out.mp4")


def test_cut_emits_select_filter():
    a = _argv(Plan(ops=[Cut(start_s=0.0, end_s=10.0)]))
    s = " ".join(a)
    assert "select='not(between(t,0.000,10.000))'" in s
    assert "[v]" in s and "[a]" in s


def test_fade_in_and_out_chain():
    a = _argv(Plan(ops=[
        FadeIn(at_s=0.0, duration_s=1.0),
        FadeOut(at_s=5.0, duration_s=1.0),
    ]))
    s = " ".join(a)
    assert "fade=t=in:st=0.000:d=1.000" in s
    assert "fade=t=out:st=5.000:d=1.000" in s


def test_speed_chains_atempo_for_factors_over_2():
    a = _argv(Plan(ops=[Speed(factor=4.0)]))
    s = " ".join(a)
    assert "atempo=2.0,atempo=2.0000" in s
    assert "setpts=0.250000*PTS" in s


def test_resize_emits_scale():
    a = _argv(Plan(ops=[Resize(width=1280, height=720)]))
    assert "scale=1280:720" in " ".join(a)


def test_volume_only_skips_video_chain():
    a = _argv(Plan(ops=[Volume(factor=0.5)]))
    s = " ".join(a)
    assert "volume=0.500" in s
    assert "[0:v:0]" not in s


def test_trim_uses_seek_args_before_output():
    a = _argv(Plan(ops=[Trim(keep_start_s=5.0, keep_end_s=12.0)]))
    s = " ".join(a)
    assert "-ss 5.000" in s and "-t 7.000" in s
    assert a[-1] == "out.mp4"


def test_output_is_last_token():
    a = _argv(Plan(ops=[FadeIn(at_s=0.0, duration_s=1.0)]))
    assert a[-1] == "out.mp4"


def test_speed_below_half_chains_atempo_minimum():
    """atempo=0.25 — must chain 0.5 + 0.5 (atempo's lower bound)."""
    a = _argv(Plan(ops=[Speed(factor=0.25)]))
    assert "atempo=0.5,atempo=0.5000" in " ".join(a)
