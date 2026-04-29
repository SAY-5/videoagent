"""v2: streaming planner events."""

from __future__ import annotations

from fastapi.testclient import TestClient

from videoagent.api import build_app
from videoagent.planner import FakeChatClient, fake_tool_calls, plan
from videoagent.verifier import SourceProbe


def _src() -> SourceProbe:
    return SourceProbe(duration_s=60.0, width=1920, height=1080, fps=30.0)


def test_planner_emits_probe_then_llm_then_plan_ready():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    events: list[dict] = []
    res = plan(chat, "cut first 10s", _src(),
               on_event=lambda e: events.append(e))
    assert res.plan is not None
    types = [e["type"] for e in events]
    assert types[0] == "probe"
    assert "llm_call" in types
    assert "tool_calls" in types
    assert types[-1] == "plan_ready"


def test_planner_streams_verify_fail_before_replan():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 30.0})),
    ])
    events: list[dict] = []
    plan(chat, "cut", _src(), on_event=lambda e: events.append(e))
    types = [e["type"] for e in events]
    # Order: probe, llm_call(0), tool_calls(0), verify_fail, llm_call(1),
    # tool_calls(1), plan_ready.
    assert "verify_fail" in types
    vf_idx = types.index("verify_fail")
    plan_ready_idx = types.index("plan_ready")
    assert vf_idx < plan_ready_idx


def test_planner_emits_plan_failed_when_budget_exhausted():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
    ])
    events: list[dict] = []
    plan(chat, "cut", _src(), on_event=lambda e: events.append(e))
    types = [e["type"] for e in events]
    assert types[-1] == "plan_failed"
    assert "verify_fail" in types


def test_sse_endpoint_delivers_full_event_sequence():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    client = TestClient(build_app(chat=chat))
    body = {
        "source_url": "s3://x", "instruction": "cut first 10s",
        "duration_s": 60.0,
    }
    with client.stream("POST", "/v1/plan/stream", json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = b"".join(r.iter_bytes()).decode()
    for ev in ("job", "probe", "llm_call", "tool_calls", "plan_ready", "end"):
        assert f"event: {ev}" in text, f"missing event {ev}: {text[:300]}"


def test_sse_endpoint_emits_verify_fail_then_plan_ready():
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 30.0})),
    ])
    client = TestClient(build_app(chat=chat))
    with client.stream("POST", "/v1/plan/stream", json={
        "source_url": "s3://x", "instruction": "cut",
        "duration_s": 60.0,
    }) as r:
        text = b"".join(r.iter_bytes()).decode()
    assert "event: verify_fail" in text
    assert "event: plan_ready" in text
    # Order check.
    assert text.index("event: verify_fail") < text.index("event: plan_ready")


def test_sse_job_id_persists_for_subsequent_get():
    """The job id emitted on the stream is also queryable via GET
    /v1/jobs/{id} so the UI can bookmark / link to it."""
    import re
    chat = FakeChatClient(responses=[
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    client = TestClient(build_app(chat=chat))
    with client.stream("POST", "/v1/plan/stream", json={
        "source_url": "s3://x", "instruction": "cut first 10s",
        "duration_s": 60.0,
    }) as r:
        text = b"".join(r.iter_bytes()).decode()
    m = re.search(r'\{"job_id":\s*"(j_[a-z0-9]+)"\}', text)
    assert m, text[:200]
    job_id = m.group(1)
    # The job is registered (status will be ready since the planner
    # ran as part of the stream).
    detail = client.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "ready"
    assert detail["plan"]["ops"][0]["op"] == "cut"
