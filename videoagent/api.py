"""FastAPI surface — submit jobs, poll status, read plans."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .planner import ChatClient, FakeChatClient, PlannerConfig, fake_tool_calls, plan
from .verifier import SourceProbe


class SubmitBody(BaseModel):
    source_url: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1, max_length=4096)
    # In production we'd ffprobe the source. For v1 the client passes
    # the probed properties directly so the API stays subprocess-free.
    duration_s: float = Field(..., gt=0)
    width:  int = Field(default=1920, gt=0, le=8192)
    height: int = Field(default=1080, gt=0, le=8192)
    fps:    float = Field(default=30.0, gt=0)
    has_audio: bool = True


@dataclass
class Job:
    id: str
    status: str = "queued"      # queued | planning | ready | failed
    plan: dict[str, Any] | None = None
    error: str = ""
    raw_calls: list[dict[str, Any]] = field(default_factory=list)
    submit: dict[str, Any] = field(default_factory=dict)


def build_app(
    chat: ChatClient | None = None,
    config: PlannerConfig | None = None,
) -> FastAPI:
    state: dict[str, Any] = {
        "chat": chat or _default_chat(),
        "config": config or PlannerConfig(),
        "jobs": {},  # id → Job
        "lock": threading.Lock(),
    }

    app = FastAPI(title="VideoAgent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("VIDEOAGENT_CORS", "http://localhost:5173").split(","),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/v1/jobs")
    def submit(body: SubmitBody) -> dict[str, Any]:
        job = Job(id="j_" + uuid.uuid4().hex[:12], submit=body.model_dump())
        with state["lock"]:
            state["jobs"][job.id] = job
        # Plan synchronously for the demo. The Go pipeline picks up
        # ready jobs from the queue (state["jobs"]) and executes
        # FFmpeg; that runs out-of-process in real deployments.
        _plan_now(state, job)
        return {"job_id": job.id, "status": job.status}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        with state["lock"]:
            j = state["jobs"].get(job_id)
        if j is None:
            raise HTTPException(404, "job not found")
        return _job_to_dict(j)

    @app.get("/v1/jobs/{job_id}/stream")
    def stream(job_id: str) -> StreamingResponse:
        """SSE: terminal-status frame for an already-submitted job.
        Live planner trace lives at POST /v1/plan/stream below."""
        def gen() -> Iterator[bytes]:
            with state["lock"]:
                j = state["jobs"].get(job_id)
            if j is None:
                yield b"event: error\ndata: {\"error\": \"not found\"}\n\n"
                return
            import json as _json
            yield ("event: " + j.status + "\ndata: "
                   + _json.dumps(_job_to_dict(j)) + "\n\n").encode()
            yield b"event: end\ndata: {}\n\n"
        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/plan/stream")
    def plan_stream(body: SubmitBody) -> StreamingResponse:
        """v2: stream planner events as SSE while the LLM call + verify
        loop runs. Frames in arrival order:
            job → probe → llm_call → tool_calls → verify_fail?
            → plan_ready | plan_failed → end
        The UI renders the trace as decisions land. The job is also
        registered in state['jobs'] so GET /v1/jobs/{id} keeps working
        for the same id."""
        from .planner import plan as run_plan
        job = Job(id="j_" + uuid.uuid4().hex[:12], submit=body.model_dump())
        with state["lock"]:
            state["jobs"][job.id] = job

        def gen() -> Iterator[bytes]:
            import json as _json
            queue: list[dict[str, Any]] = []
            cv = threading.Condition()
            done = threading.Event()

            def on_event(ev: dict[str, Any]) -> None:
                with cv:
                    queue.append(ev)
                    cv.notify()

            src = SourceProbe(
                duration_s=body.duration_s,
                width=body.width,
                height=body.height,
                fps=body.fps,
                has_audio=body.has_audio,
            )

            def runner() -> None:
                try:
                    res = run_plan(
                        state["chat"], body.instruction, src,
                        state["config"], on_event=on_event,
                    )
                    if res.plan is not None:
                        job.status = "ready"
                        job.plan = res.plan.model_dump()
                    else:
                        job.status = "failed"
                        job.error = "; ".join(f"{e.code}: {e.hint}" for e in res.errors)
                    job.raw_calls = res.raw_calls
                finally:
                    done.set()
                    with cv:
                        cv.notify()

            threading.Thread(target=runner, daemon=True).start()
            yield (f"event: job\ndata: {_json.dumps({'job_id': job.id})}\n\n").encode()
            while True:
                with cv:
                    while not queue and not done.is_set():
                        cv.wait()
                    drained = queue[:]
                    del queue[:]
                for ev in drained:
                    yield (f"event: {ev['type']}\ndata: {_json.dumps(ev, default=str)}\n\n").encode()
                if done.is_set() and not queue:
                    break
            yield b"event: end\ndata: {}\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return _ROOT_HTML

    return app


def _plan_now(state: dict[str, Any], job: Job) -> None:
    job.status = "planning"
    src = SourceProbe(
        duration_s=job.submit["duration_s"],
        width=job.submit["width"],
        height=job.submit["height"],
        fps=job.submit["fps"],
        has_audio=job.submit["has_audio"],
    )
    res = plan(state["chat"], job.submit["instruction"], src, state["config"])
    job.raw_calls = res.raw_calls
    if res.plan is None:
        job.status = "failed"
        job.error = "; ".join(f"{e.code}: {e.hint}" for e in res.errors)
        return
    job.plan = res.plan.model_dump()
    job.status = "ready"


def _job_to_dict(j: Job) -> dict[str, Any]:
    return {
        "id": j.id,
        "status": j.status,
        "plan": j.plan,
        "error": j.error,
        "raw_calls": j.raw_calls,
        "submit": j.submit,
    }


def _default_chat() -> ChatClient:
    """If OPENAI_API_KEY is set, wire the real client. Otherwise use a
    deterministic fake that emits a simple cut for any 'cut N' query."""
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from .openai_client import OpenAIChatClient
            return OpenAIChatClient(
                model=os.environ.get("VIDEOAGENT_MODEL", "gpt-4o-mini"),
            )
        except Exception:
            pass
    return _StubChatClient()


class _StubChatClient:
    """Deterministic fallback: parses simple intents from the user
    message so the service runs end-to-end without OpenAI.
    Patterns:
      'cut first <N> seconds'  → Cut(0, N)
      'fade in at <T>'         → FadeIn(T, 1.0)
      'fade out at <T>'        → FadeOut(T, 1.0)
      else                     → empty plan."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        import re
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        calls: list[tuple[str, dict[str, Any]]] = []
        m = re.search(r"cut\s+(?:the\s+)?first\s+([\d.]+)\s*seconds?", user, re.I)
        if m:
            calls.append(("cut", {"start_s": 0.0, "end_s": float(m.group(1))}))
        m = re.search(r"fade\s+in\s+at\s+([\d.]+)", user, re.I)
        if m:
            calls.append(("fade_in", {"at_s": float(m.group(1)), "duration_s": 1.0}))
        m = re.search(r"fade\s+out\s+at\s+([\d.]+)", user, re.I)
        if m:
            calls.append(("fade_out", {"at_s": float(m.group(1)), "duration_s": 1.0}))
        return fake_tool_calls(*calls) if calls else {
            "choices": [{"message": {"role": "assistant", "content": "(no edit)"}}],
        }


_ROOT_HTML = """<!doctype html>
<html><body style="font-family: ui-monospace, monospace; max-width: 720px; margin: 40px auto; padding: 0 16px;">
<h1>VideoAgent API</h1>
<p>POST <code>/v1/jobs</code> with <code>{source_url, instruction, duration_s, width, height, fps, has_audio}</code>.</p>
<p>Timeline UI: <a href="http://localhost:5173">localhost:5173</a>.</p>
</body></html>"""

# Module-level app for `uvicorn videoagent.api:app`.
app = build_app()


# Re-export for tests.
__all__ = ["FakeChatClient", "app", "build_app", "fake_tool_calls"]
