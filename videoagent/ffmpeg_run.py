"""Translate a verified Plan into an FFmpeg command line.

Each op has a single canonical FFmpeg formulation. The translator
emits an `ffmpeg -i <src> -filter_complex <chain> -map ... <out>`
invocation. Subprocess management lives in the Go pipeline; this
module just produces the argv.
"""

from __future__ import annotations

from collections.abc import Sequence

from .ops import Cut, FadeIn, FadeOut, Plan, Resize, Speed, Trim, Volume


def build_argv(input_path: str, plan: Plan, output_path: str) -> list[str]:
    """Return an argv suitable for `subprocess.run(argv, ...)`.

    The current implementation supports a single primary input plus
    Concat extras (which append `-i` flags). Filters are chained in
    plan order — order matters: `cut` then `fade_in` does not equal
    `fade_in` then `cut`.
    """
    argv: list[str] = ["ffmpeg", "-y", "-i", input_path]
    extra_inputs: list[str] = []
    video_filters: list[str] = []
    audio_filters: list[str] = []
    has_audio_filter = False
    has_video_filter = False
    needs_concat: list[str] = []  # extra source paths from a Concat op
    seek_args: list[str] = []     # -ss / -t at the *start* if present

    for op in plan.ops:
        if isinstance(op, Cut):
            # Express as a `select` filter that drops [start, end).
            video_filters.append(
                f"select='not(between(t,{op.start_s:.3f},{op.end_s:.3f}))',setpts=N/FRAME_RATE/TB"
            )
            audio_filters.append(
                f"aselect='not(between(t,{op.start_s:.3f},{op.end_s:.3f}))',asetpts=N/SR/TB"
            )
            has_video_filter = True
            has_audio_filter = True
        elif isinstance(op, Trim):
            seek_args = ["-ss", f"{op.keep_start_s:.3f}",
                         "-t", f"{op.keep_end_s - op.keep_start_s:.3f}"]
        elif isinstance(op, FadeIn):
            video_filters.append(f"fade=t=in:st={op.at_s:.3f}:d={op.duration_s:.3f}")
            has_video_filter = True
        elif isinstance(op, FadeOut):
            video_filters.append(f"fade=t=out:st={op.at_s:.3f}:d={op.duration_s:.3f}")
            has_video_filter = True
        elif isinstance(op, Speed):
            # Video: setpts; audio: atempo (which has its own bounds).
            video_filters.append(f"setpts={1.0 / op.factor:.6f}*PTS")
            audio_filters.append(_atempo_chain(op.factor))
            has_video_filter = True
            has_audio_filter = True
        elif isinstance(op, Volume):
            audio_filters.append(f"volume={op.factor:.3f}")
            has_audio_filter = True
        elif isinstance(op, Resize):
            video_filters.append(f"scale={op.width}:{op.height}")
            has_video_filter = True
        else:
            # Concat extras get prepended as additional -i inputs.
            for src in getattr(op, "inputs", []) or []:
                needs_concat.append(src)

    for src in needs_concat:
        extra_inputs += ["-i", src]
    # Insert extra inputs just before -filter_complex so ffmpeg
    # numbers them after the primary source.
    if extra_inputs:
        argv.extend(extra_inputs)
    if needs_concat:
        # n+1 streams (primary + extras), v=1, a=1.
        n = 1 + len(needs_concat)
        chain = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
        argv += ["-filter_complex", chain, "-map", "[v]", "-map", "[a]"]
    elif has_video_filter or has_audio_filter:
        parts: list[str] = []
        if has_video_filter:
            parts.append(",".join(video_filters) + "[v]")
            argv += ["-filter_complex", ";".join(parts) if False else parts[-1]]
            # The above writes only video; if we also have audio, append.
        # Simpler unified path: build a single -filter_complex string.
        argv = ["ffmpeg", "-y", "-i", input_path]
        complex_parts: list[str] = []
        if has_video_filter:
            complex_parts.append("[0:v:0]" + ",".join(video_filters) + "[v]")
        if has_audio_filter:
            complex_parts.append("[0:a:0]" + ",".join(audio_filters) + "[a]")
        argv += ["-filter_complex", ";".join(complex_parts)]
        if has_video_filter:
            argv += ["-map", "[v]"]
        if has_audio_filter:
            argv += ["-map", "[a]"]
    if seek_args:
        # -ss / -t belong before the output path; insert them now.
        argv += seek_args
    argv.append(output_path)
    return argv


def _atempo_chain(factor: float) -> str:
    """FFmpeg's atempo only accepts factors in [0.5, 100]. For
    requests outside [0.5, 2] we chain multiple atempos to stay in-
    range — e.g. 4x = atempo=2,atempo=2."""
    if 0.5 <= factor <= 2.0:
        return f"atempo={factor:.4f}"
    if factor < 0.5:
        return f"atempo=0.5,atempo={factor / 0.5:.4f}"
    # factor > 2.0 — chain factors of 2 until we're under 2.
    chain: list[str] = []
    f = factor
    while f > 2.0:
        chain.append("atempo=2.0")
        f /= 2.0
    chain.append(f"atempo={f:.4f}")
    return ",".join(chain)


def explain(argv: Sequence[str]) -> str:
    """Human-readable summary of an argv. Used in logs + the API
    response so operators can see what FFmpeg actually ran."""
    out = []
    for tok in argv:
        if any(c in tok for c in (" ", "[", "]", "'", "*")):
            out.append(repr(tok))
        else:
            out.append(tok)
    return " ".join(out)
