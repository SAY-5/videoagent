"""Runtime verification: does this op plan make sense for THIS source?

Pydantic catches type errors and out-of-range values at parse time.
The remaining failures depend on the source video's actual properties
— a Cut(end_s=200) is structurally valid but breaks against a
120-second clip. The Verifier runs after Pydantic, before FFmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ops import Cut, FadeIn, FadeOut, Plan, Resize, Speed, Trim, Volume


@dataclass
class SourceProbe:
    """The subset of `ffprobe` output the verifier cares about."""

    duration_s: float
    has_audio: bool = True
    has_video: bool = True
    width:  int = 0
    height: int = 0
    fps:    float = 0.0


@dataclass
class VerifyError:
    op_idx: int
    code: str
    hint: str        # human-readable + actionable; fed back to the LLM


def verify(plan: Plan, source: SourceProbe) -> list[VerifyError]:
    """Return a list of structured errors. Empty == plan is runnable."""
    errors: list[VerifyError] = []
    for i, op in enumerate(plan.ops):
        errors.extend(_verify_one(i, op, source))
    return errors


def _verify_one(i: int, op, source: SourceProbe) -> list[VerifyError]:
    out: list[VerifyError] = []

    if isinstance(op, Cut):
        if op.end_s > source.duration_s + 1e-3:
            out.append(VerifyError(
                op_idx=i, code="cut_end_past_source",
                hint=(
                    f"Cut.end_s ({op.end_s:.3f}s) is past the source's "
                    f"duration ({source.duration_s:.3f}s). Pick a value "
                    f"within [0, {source.duration_s:.3f}]."
                ),
            ))
        if op.start_s >= source.duration_s:
            out.append(VerifyError(
                op_idx=i, code="cut_start_past_source",
                hint=f"Cut.start_s ({op.start_s:.3f}s) >= source duration ({source.duration_s:.3f}s).",
            ))

    elif isinstance(op, Trim):
        if op.keep_end_s > source.duration_s + 1e-3:
            out.append(VerifyError(
                op_idx=i, code="trim_end_past_source",
                hint=f"Trim.keep_end_s ({op.keep_end_s:.3f}s) past source ({source.duration_s:.3f}s).",
            ))
        if op.keep_end_s - op.keep_start_s < 0.1:
            out.append(VerifyError(
                op_idx=i, code="trim_too_short",
                hint=(
                    f"Trim keeps only {op.keep_end_s - op.keep_start_s:.3f}s; "
                    "must keep at least 100ms of usable footage."
                ),
            ))

    elif isinstance(op, FadeIn):
        if op.at_s + op.duration_s > source.duration_s + 1e-3:
            out.append(VerifyError(
                op_idx=i, code="fade_past_source",
                hint=(
                    f"FadeIn ends at {op.at_s + op.duration_s:.3f}s; "
                    f"source is {source.duration_s:.3f}s long."
                ),
            ))
    elif isinstance(op, FadeOut):
        if op.at_s + op.duration_s > source.duration_s + 1e-3:
            out.append(VerifyError(
                op_idx=i, code="fade_past_source",
                hint=(
                    f"FadeOut ends at {op.at_s + op.duration_s:.3f}s; "
                    f"source is {source.duration_s:.3f}s long."
                ),
            ))

    elif isinstance(op, Speed):
        # New duration = source / factor. factor < 1 means slower
        # (longer playback). Refuse anything that would produce an
        # absurdly long output.
        new_duration = source.duration_s / op.factor
        if new_duration > 6 * 3600:
            out.append(VerifyError(
                op_idx=i, code="speed_too_long",
                hint=(
                    f"Speed factor {op.factor} on {source.duration_s:.0f}s "
                    f"source would yield {new_duration / 3600:.1f}h of output."
                ),
            ))

    elif isinstance(op, Volume):
        if not source.has_audio:
            out.append(VerifyError(
                op_idx=i, code="volume_no_audio",
                hint="Volume change requested but the source has no audio stream.",
            ))

    elif isinstance(op, Resize):
        # Pydantic already enforces even dimensions; nothing source-
        # dependent to check here. Future: warn on very large up-
        # scales (> 4x linear) since FFmpeg quality degrades.
        pass

    # Concat is verified at the pipeline layer (probe each input).

    return out


def errors_for_replan(errors: list[VerifyError]) -> str:
    """Human-readable error list to feed back into the planner for
    the bounded retry. The LLM has been observed to fix these on the
    first re-prompt about half the time on the curated eval set."""
    if not errors:
        return ""
    bullets = [f"- op #{e.op_idx} ({e.code}): {e.hint}" for e in errors]
    return (
        "Your previous plan failed verification against the source. "
        "Fix these specific problems and emit a new plan:\n"
        + "\n".join(bullets)
    )
