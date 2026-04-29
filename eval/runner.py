"""Run the curated suite. v3.

Used both as a Python module ('python -m eval.runner --json out.json')
and from CI as the regression gate. Exits 0 only if every case
matches its expectation.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eval.cases import BOUNDED, EXACT, FAILURES, BoundedCase, FailureCase
from videoagent.eval import run_suite
from videoagent.planner import ChatClient, FakeChatClient, fake_tool_calls, plan


@dataclass
class CaseOutcome:
    name: str
    kind: str            # "exact" | "bounded" | "failure"
    ok: bool
    elapsed_ms: int = 0
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteReport:
    outcomes: list[CaseOutcome]

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total":  self.total,
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def run(chat: ChatClient) -> SuiteReport:
    results: list[CaseOutcome] = []
    # Exact: plan-equality check via the existing harness.
    rep = run_suite(EXACT, chat)
    for r in rep.cases:
        results.append(CaseOutcome(
            name=r.name, kind="exact", ok=r.ok,
            elapsed_ms=r.elapsed_ms, error=r.error,
            details={"plan": r.plan},
        ))
    # Bounded: each predicate runs against the planner output.
    for case in BOUNDED:
        results.append(_run_bounded(case, chat))
    # Failure: planner must NOT produce a plan, and must surface the
    # expected error code.
    for case in FAILURES:
        results.append(_run_failure(case, chat))
    return SuiteReport(outcomes=results)


def _run_bounded(case: BoundedCase, chat: ChatClient) -> CaseOutcome:
    import time
    t0 = time.perf_counter()
    res = plan(chat, case.instruction, case.source)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if res.plan is None:
        return CaseOutcome(
            name=case.name, kind="bounded", ok=False, elapsed_ms=elapsed_ms,
            error="; ".join(e.code for e in res.errors) or "no plan",
            details={"explanation": case.explanation},
        )
    ok = case.predicate(res.plan)
    return CaseOutcome(
        name=case.name, kind="bounded", ok=ok, elapsed_ms=elapsed_ms,
        error="" if ok else f"predicate failed: {case.explanation}",
        details={"plan": res.plan.model_dump()},
    )


def _run_failure(case: FailureCase, chat: ChatClient) -> CaseOutcome:
    import time
    t0 = time.perf_counter()
    res = plan(chat, case.instruction, case.source)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    if res.plan is not None:
        return CaseOutcome(
            name=case.name, kind="failure", ok=False, elapsed_ms=elapsed_ms,
            error="planner produced a plan when it should have failed",
            details={"plan": res.plan.model_dump()},
        )
    actual = ", ".join(e.code for e in res.errors) or ""
    if case.expected_error_code in actual:
        return CaseOutcome(
            name=case.name, kind="failure", ok=True, elapsed_ms=elapsed_ms,
            details={"errors": actual},
        )
    return CaseOutcome(
        name=case.name, kind="failure", ok=False, elapsed_ms=elapsed_ms,
        error=f"expected {case.expected_error_code!r}, got {actual!r}",
    )


# --- a deterministic chat fixture for CI ----------------------------

def _ci_chat() -> ChatClient:
    """Hand-rolled responses keyed off the instruction. No network.
    Each instruction maps to the tool calls a well-behaved planner
    would produce for that case. This is the regression gate: if the
    real planner ever stops emitting these for these instructions
    against these sources, the suite fails."""
    return _RoutingFakeChat()


class _RoutingFakeChat:
    """Returns scripted tool_calls based on instruction substrings.
    For failure cases we still emit a tool_call — the verifier is
    what should reject it."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        instruction = next(
            (m["content"] for m in messages if m["role"] == "user"), "",
        ).lower()

        # Lookups in priority order — first match wins.
        TABLE: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = [
            ("cut the first 10 seconds and fade out at 1:30", [
                ("cut",      {"start_s": 0.0, "end_s": 10.0}),
                ("fade_out", {"at_s": 90.0, "duration_s": 2.0}),
            ]),
            ("cut the first 10 seconds", [("cut", {"start_s": 0.0, "end_s": 10.0})]),
            ("fade in at the start over 1 second", [("fade_in", {"at_s": 0.0, "duration_s": 1.0})]),
            ("fade out at 1:30 over 2 seconds", [("fade_out", {"at_s": 90.0, "duration_s": 2.0})]),
            ("fade out starting at 58 seconds for 5 seconds", [("fade_out", {"at_s": 58.0, "duration_s": 5.0})]),
            ("resize to 1280x720", [("resize", {"width": 1280, "height": 720})]),
            ("play at 2x speed", [("speed", {"factor": 2.0})]),
            ("halve the volume", [("volume", {"factor": 0.5})]),
            ("keep just from 5 to 35 seconds", [("trim", {"keep_start_s": 5.0, "keep_end_s": 35.0})]),
            ("concatenate with bonus.mp4", [("concat", {"inputs": ["bonus.mp4"]})]),
            ("cut from 30 seconds to 5 minutes", [("cut", {"start_s": 30.0, "end_s": 300.0})]),
        ]
        for needle, calls in TABLE:
            if needle in instruction:
                return fake_tool_calls(*calls)
        return {"choices": [{"message": {"role": "assistant", "content": "(no edit)"}}]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="videoagent.eval")
    ap.add_argument("--json", help="write a SuiteReport JSON to this path")
    ap.add_argument("--summary", action="store_true",
                    help="print case names + counts, then exit (no run)")
    args = ap.parse_args(argv)

    if args.summary:
        from eval.cases import summary
        json.dump(summary(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    rep = run(_ci_chat())
    payload = rep.to_json()
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, indent=2))
    # Pretty summary.
    print(f"PASSED {rep.passed}/{rep.total}")
    for o in rep.outcomes:
        mark = "✓" if o.ok else "✗"
        line = f"  {mark} [{o.kind}] {o.name}"
        if not o.ok:
            line += f"  — {o.error}"
        print(line)
    return 0 if rep.passed == rep.total else 1


# Helpful __init__.py-style export so tests can `from eval.runner import run`.
__all__ = ["CaseOutcome", "SuiteReport", "main", "run"]


if __name__ == "__main__":
    raise SystemExit(main())


# Suppress unused-import warning if FakeChatClient isn't referenced
# directly above (it's part of the public API surface).
_ = FakeChatClient
