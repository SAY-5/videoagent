"""Eval harness: runs a fixed set of (instruction, source, expected)
tuples through the planner, executes the resulting plan, and checks
the output against frame-level expectations.

The eval harness is the feedback loop. It's the only thing that
catches the "valid-looking but wrong timecode" failure mode where
the planner produces a plan that passes Pydantic + the verifier but
still does the wrong edit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .ops import Plan
from .planner import ChatClient, PlannerConfig
from .planner import plan as run_planner
from .verifier import SourceProbe


@dataclass
class EvalCase:
    name: str
    instruction: str
    source: SourceProbe
    expected_plan: Plan | None = None             # if set, asserts plan equality
    expected_frame_md5: dict[float, str] = field(default_factory=dict)  # ts → md5


@dataclass
class CaseResult:
    name: str
    ok: bool
    elapsed_ms: int
    plan: dict[str, Any] | None = None
    error: str = ""
    frame_diffs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EvalReport:
    cases: list[CaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.ok)

    @property
    def total(self) -> int:
        return len(self.cases)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total":  self.total,
            "cases":  [asdict(c) for c in self.cases],
        }


def run_suite(
    cases: list[EvalCase],
    chat: ChatClient,
    *,
    config: PlannerConfig | None = None,
    output_dir: Path | None = None,
    runner: Callable[[Plan, EvalCase, Path], Path | None] | None = None,
) -> EvalReport:
    """Drive every case through the planner. If `runner` is supplied,
    it executes the plan and returns the output path; the harness
    then samples frames at expected_frame_md5 timestamps and compares.

    `runner=None` skips FFmpeg execution — useful for unit tests that
    just want to assert the plan shape."""
    cfg = config or PlannerConfig()
    out_dir = output_dir or Path("./eval-out")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[CaseResult] = []
    for case in cases:
        import time
        t0 = time.perf_counter()
        result = run_planner(chat, case.instruction, case.source, cfg)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if result.plan is None:
            results.append(CaseResult(
                name=case.name, ok=False, elapsed_ms=elapsed_ms,
                error="; ".join(e.code for e in result.errors) or "no plan",
            ))
            continue

        # Plan-equality check.
        if case.expected_plan is not None:
            if _plan_equal(result.plan, case.expected_plan):
                results.append(CaseResult(
                    name=case.name, ok=True, elapsed_ms=elapsed_ms,
                    plan=result.plan.model_dump(),
                ))
                continue
            results.append(CaseResult(
                name=case.name, ok=False, elapsed_ms=elapsed_ms,
                plan=result.plan.model_dump(),
                error=(
                    f"plan mismatch: got {result.plan.model_dump()} "
                    f"expected {case.expected_plan.model_dump()}"
                ),
            ))
            continue

        # Frame-level check (requires runner + ffmpeg).
        if runner is None:
            # Plan accepted, no execution requested.
            results.append(CaseResult(
                name=case.name, ok=True, elapsed_ms=elapsed_ms,
                plan=result.plan.model_dump(),
            ))
            continue

        try:
            output = runner(result.plan, case, out_dir)
            if output is None:
                results.append(CaseResult(
                    name=case.name, ok=False, elapsed_ms=elapsed_ms,
                    plan=result.plan.model_dump(),
                    error="runner returned no output path",
                ))
                continue
            diffs = _check_frames(output, case.expected_frame_md5)
            results.append(CaseResult(
                name=case.name, ok=not diffs, elapsed_ms=elapsed_ms,
                plan=result.plan.model_dump(),
                frame_diffs=diffs,
            ))
        except Exception as e:
            results.append(CaseResult(
                name=case.name, ok=False, elapsed_ms=elapsed_ms,
                plan=result.plan.model_dump(), error=str(e),
            ))

    return EvalReport(cases=results)


def _plan_equal(a: Plan, b: Plan) -> bool:
    """Plan equality compares op-by-op with a small float tolerance.
    The LLM occasionally emits 10.0 vs 10 for the same intended value
    — a strict equality misses that they're the same plan."""
    if len(a.ops) != len(b.ops):
        return False
    for op_a, op_b in zip(a.ops, b.ops, strict=True):
        if type(op_a) is not type(op_b):
            return False
        for name, _info in type(op_a).model_fields.items():
            if name == "op":
                continue
            va = getattr(op_a, name)
            vb = getattr(op_b, name)
            if isinstance(va, float) and isinstance(vb, float):
                if abs(va - vb) > 1e-3:
                    return False
            elif va != vb:
                return False
    return True


def _check_frames(output: Path, expected: dict[float, str]) -> list[dict[str, Any]]:
    """Sample frames at the requested timestamps and compare md5
    against expected. Requires `ffmpeg` on PATH."""
    diffs: list[dict[str, Any]] = []
    for ts, want_md5 in expected.items():
        got = _frame_md5(output, ts)
        if got != want_md5:
            diffs.append({"ts": ts, "expected": want_md5, "got": got})
    return diffs


def _frame_md5(video: Path, ts: float) -> str:
    """Extract a single frame at `ts` as PNG and md5 it. The PNG
    encoding is deterministic enough that the md5 is a stable
    signature of the visual frame."""
    proc = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y",
            "-ss", f"{ts:.3f}", "-i", str(video),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
        ],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return hashlib.md5(proc.stdout, usedforsecurity=False).hexdigest()


def write_report(rep: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep.to_json(), indent=2))
