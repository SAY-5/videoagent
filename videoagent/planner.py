"""LLM planner: probe → tool-call → parse → verify → (one retry).

The planner is intentionally small. The reliability comes from the
op schemas (constrained Pydantic), the verifier (source-aware), and
the eval harness (frame-level ground truth) — not from prompt
complexity. The planner just stitches them together.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .ops import ALL_OPS, Plan
from .verifier import SourceProbe, VerifyError, errors_for_replan, verify

# --- chat-client protocol --------------------------------------------

class ChatClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


# --- planner ----------------------------------------------------------

@dataclass
class PlannerConfig:
    model: str = "gpt-4o-mini"
    max_replans: int = 1
    max_ops: int = 32


@dataclass
class PlanResult:
    plan: Plan | None
    errors: list[VerifyError] = field(default_factory=list)
    raw_calls: list[dict[str, Any]] = field(default_factory=list)
    replans: int = 0


def tool_schemas() -> list[dict[str, Any]]:
    """Generate OpenAI function-calling schemas from the Pydantic
    op classes. Single source of truth: every constraint we want
    enforced lives on the model itself."""
    out: list[dict[str, Any]] = []
    for cls in ALL_OPS:
        schema = cls.model_json_schema()
        # OpenAI's function-call schema sits inside a wrapper.
        out.append({
            "type": "function",
            "function": {
                "name": cls.model_fields["op"].default,
                "description": (cls.__doc__ or "").strip(),
                "parameters": _strip_pydantic_internals(schema),
            },
        })
    return out


def _strip_pydantic_internals(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic emits schemas with `$defs`, `title`, etc. that OpenAI
    accepts but doesn't need. Trim them so the schema is auditable
    in logs."""
    # Drop the discriminator literal `op` from the parameters: OpenAI
    # already knows the function name, repeating it is noise.
    schema = dict(schema)
    schema.pop("title", None)
    schema.pop("$defs", None)
    props = dict(schema.get("properties", {}))
    props.pop("op", None)
    schema["properties"] = props
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "op"]
    return schema


def plan(
    chat: ChatClient,
    instruction: str,
    source: SourceProbe,
    config: PlannerConfig | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> PlanResult:
    """Plan a sequence of FFmpeg ops.

    on_event (v2) is called with structured events as the planner
    progresses: probe → llm_call → tool_calls → verify_ok|verify_fail
    → replan? → plan_ready|plan_failed. The HTTP layer translates each
    event into an SSE frame so the UI fills in the trace live."""
    cfg = config or PlannerConfig()
    messages: list[dict[str, Any]] = _initial_messages(instruction, source)
    raw_calls: list[dict[str, Any]] = []
    schemas = tool_schemas()
    last_errors: list[VerifyError] = []
    if on_event is not None:
        on_event({
            "type": "probe",
            "duration_s": source.duration_s,
            "width": source.width,
            "height": source.height,
            "fps": source.fps,
            "has_audio": source.has_audio,
        })

    for replans in range(cfg.max_replans + 1):
        if on_event is not None:
            on_event({"type": "llm_call", "attempt": replans})
        resp = chat.complete(messages=messages, tools=schemas)
        choice = resp["choices"][0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls") or []
        raw_calls.extend(tool_calls)
        if on_event is not None:
            on_event({
                "type": "tool_calls",
                "attempt": replans,
                "calls": [
                    {"name": (tc.get("function") or {}).get("name", ""),
                     "args": (tc.get("function") or {}).get("arguments", "{}")}
                    for tc in tool_calls
                ],
            })

        try:
            p = _parse_plan(tool_calls, max_ops=cfg.max_ops)
        except ValidationError as e:
            last_errors = [VerifyError(op_idx=-1, code="schema_invalid",
                                       hint=str(e).splitlines()[-1])]
            messages = _replan_messages(instruction, source, last_errors)
            continue
        except ValueError as e:
            # Unknown tool name, malformed JSON args, or too-many-ops
            # — treat the same as a schema violation. Feed the error
            # back and let the LLM retry once.
            last_errors = [VerifyError(op_idx=-1, code="bad_tool_call", hint=str(e))]
            messages = _replan_messages(instruction, source, last_errors)
            continue

        last_errors = verify(p, source)
        if not last_errors:
            if on_event is not None:
                on_event({
                    "type": "plan_ready",
                    "attempt": replans,
                    "ops": [op.model_dump() for op in p.ops],
                })
            return PlanResult(plan=p, errors=[], raw_calls=raw_calls, replans=replans)

        # Verification failed — feed the structured errors back and try once more.
        if on_event is not None:
            on_event({
                "type": "verify_fail",
                "attempt": replans,
                "errors": [
                    {"op_idx": e.op_idx, "code": e.code, "hint": e.hint}
                    for e in last_errors
                ],
            })
        messages = _replan_messages(instruction, source, last_errors)

    if on_event is not None:
        on_event({
            "type": "plan_failed",
            "errors": [
                {"op_idx": e.op_idx, "code": e.code, "hint": e.hint}
                for e in last_errors
            ],
        })
    return PlanResult(plan=None, errors=last_errors, raw_calls=raw_calls, replans=cfg.max_replans)


def _parse_plan(tool_calls: list[dict[str, Any]], max_ops: int) -> Plan:
    """Each tool_call's name is the op type; arguments JSON parses
    into the matching Pydantic model. Pydantic raises on out-of-range
    values."""
    if len(tool_calls) > max_ops:
        raise ValidationError.from_exception_data("Plan", [{
            "type": "value_error",
            "loc": (),
            "msg": f"too many ops: {len(tool_calls)} > {max_ops}",
            "input": tool_calls,
        }])
    by_name = {cls.model_fields["op"].default: cls for cls in ALL_OPS}
    parsed = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        cls = by_name.get(name)
        if cls is None:
            raise ValueError(f"unknown op: {name}")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"bad json args for {name}: {e}") from e
        # Pydantic validation. Ranges + custom validators run here.
        parsed.append(cls(**args, op=name))
    return Plan(ops=parsed)


_BASE_SYSTEM = (
    "You are a video-editing planner. Given a user instruction and a "
    "probed source video, emit one or more FFmpeg operations as tool "
    "calls. Use ONLY the provided tools — do not invent new operations.\n"
    "Constraints:\n"
    " - Every timecode must be within the source's duration.\n"
    " - Cuts must satisfy start_s < end_s.\n"
    " - Volume changes require an audio stream.\n"
    " - Resize dimensions must be even.\n"
    "If the user's intent is ambiguous, prefer the conservative edit "
    "(shorter cut, smaller fade) and explain in plain text only if NO "
    "tool fits."
)


def _initial_messages(instruction: str, source: SourceProbe) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _BASE_SYSTEM + _source_block(source)},
        {"role": "user", "content": instruction},
    ]


def _replan_messages(
    instruction: str,
    source: SourceProbe,
    errors: list[VerifyError],
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _BASE_SYSTEM + _source_block(source)},
        {"role": "user", "content": instruction},
        {"role": "system", "content": errors_for_replan(errors)},
    ]


def _source_block(source: SourceProbe) -> str:
    return (
        f"\n\nSource probe:\n"
        f"  duration: {source.duration_s:.3f}s\n"
        f"  resolution: {source.width}x{source.height}\n"
        f"  fps: {source.fps:.2f}\n"
        f"  audio: {'yes' if source.has_audio else 'no'}"
    )


# --- a ChatClient stub used by tests ----------------------------------

@dataclass
class FakeChatClient:
    """Scripted client. responses[i] is the response to the i-th
    complete() call. Tests use `tool_calls(...)` to build entries."""
    responses: list[dict[str, Any]] = field(default_factory=list)
    sent: list[list[dict[str, Any]]] = field(default_factory=list)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.sent.append(list(messages))
        if not self.responses:
            return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": []}}]}
        return self.responses.pop(0)


def fake_tool_calls(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a chat response with the given tool calls, in order."""
    tc = []
    for i, (name, args) in enumerate(calls):
        tc.append({
            "id": f"tc_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tc}}]}


# Tiny re-export.
_ = Callable
