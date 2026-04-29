"""Op schemas. Most failures the LLM produces are caught here, at
parse time, before any verification runs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_cut_rejects_end_before_start():
    with pytest.raises(ValidationError):
        Cut(start_s=10.0, end_s=5.0)


def test_cut_rejects_equal_start_end():
    with pytest.raises(ValidationError):
        Cut(start_s=5.0, end_s=5.0)


def test_cut_negative_start_rejected():
    with pytest.raises(ValidationError):
        Cut(start_s=-1.0, end_s=5.0)


def test_trim_must_keep_more_than_zero_time():
    with pytest.raises(ValidationError):
        Trim(keep_start_s=5.0, keep_end_s=5.0)


def test_speed_factor_zero_rejected():
    with pytest.raises(ValidationError):
        Speed(factor=0.0)


def test_speed_factor_too_large_rejected():
    with pytest.raises(ValidationError):
        Speed(factor=20.0)


def test_volume_factor_negative_rejected():
    with pytest.raises(ValidationError):
        Volume(factor=-0.1)


def test_volume_factor_capped():
    with pytest.raises(ValidationError):
        Volume(factor=10.0)


@pytest.mark.parametrize("dim", [(1280, 720), (1920, 1080), (640, 480)])
def test_resize_accepts_even_dims(dim):
    Resize(width=dim[0], height=dim[1])


def test_resize_rejects_odd_width():
    with pytest.raises(ValidationError):
        Resize(width=1281, height=720)


def test_resize_rejects_zero():
    with pytest.raises(ValidationError):
        Resize(width=0, height=720)


def test_fade_durations_capped_at_10s():
    FadeIn(at_s=0.0, duration_s=10.0)
    with pytest.raises(ValidationError):
        FadeIn(at_s=0.0, duration_s=10.001)


def test_concat_requires_at_least_one_input():
    Concat(inputs=["a.mp4"])  # 1 is the documented min
    with pytest.raises(ValidationError):
        Concat(inputs=[])


def test_concat_caps_inputs_at_8():
    with pytest.raises(ValidationError):
        Concat(inputs=[f"clip{i}.mp4" for i in range(9)])


def test_plan_caps_op_count_at_32():
    ops = [Cut(start_s=i * 0.1, end_s=(i + 1) * 0.1) for i in range(32)]
    Plan(ops=ops)
    with pytest.raises(ValidationError):
        Plan(ops=[*ops, Cut(start_s=10.0, end_s=10.5)])


def test_plan_round_trip_via_json():
    p = Plan(ops=[
        Cut(start_s=0.0, end_s=10.0),
        FadeOut(at_s=80.0, duration_s=2.0),
    ])
    js = p.model_dump_json()
    again = Plan.model_validate_json(js)
    assert again == p


def test_op_dict_dispatches_via_discriminator():
    """The discriminated-union typing means a plain dict with 'op'
    routes to the right model when validated through Plan."""
    p = Plan.model_validate({"ops": [
        {"op": "cut", "start_s": 0.0, "end_s": 5.0},
        {"op": "fade_in", "at_s": 0.0, "duration_s": 1.0},
    ]})
    assert isinstance(p.ops[0], Cut)
    assert isinstance(p.ops[1], FadeIn)
