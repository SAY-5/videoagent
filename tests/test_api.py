"""HTTP layer: submit/poll/stream."""

from __future__ import annotations

from fastapi.testclient import TestClient

from videoagent.api import build_app
from videoagent.planner import FakeChatClient, fake_tool_calls


def _client(responses):
    chat = FakeChatClient(responses=responses)
    return TestClient(build_app(chat=chat))


def test_healthz():
    c = _client([])
    assert c.get("/healthz").json() == {"ok": True}


def test_submit_and_get_returns_plan():
    c = _client([
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 10.0})),
    ])
    r = c.post("/v1/jobs", json={
        "source_url": "s3://bucket/source.mp4",
        "instruction": "cut the first 10 seconds",
        "duration_s": 60.0,
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    detail = c.get(f"/v1/jobs/{job_id}").json()
    assert detail["status"] == "ready"
    assert detail["plan"]["ops"][0]["op"] == "cut"
    assert detail["plan"]["ops"][0]["end_s"] == 10.0


def test_submit_with_unverifiable_plan_marks_failed():
    """LLM emits a Cut beyond the source's duration twice — planner
    gives up; API returns status=failed with the structured error."""
    c = _client([
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 999.0})),
    ])
    r = c.post("/v1/jobs", json={
        "source_url": "x", "instruction": "cut everything",
        "duration_s": 60.0,
    })
    detail = c.get(f"/v1/jobs/{r.json()['job_id']}").json()
    assert detail["status"] == "failed"
    assert "cut_end_past_source" in detail["error"]


def test_get_unknown_job_404():
    c = _client([])
    assert c.get("/v1/jobs/does-not-exist").status_code == 404


def test_stream_emits_terminal_status_event():
    c = _client([
        fake_tool_calls(("cut", {"start_s": 0.0, "end_s": 5.0})),
    ])
    job_id = c.post("/v1/jobs", json={
        "source_url": "s3://x", "instruction": "cut first 5s",
        "duration_s": 60.0,
    }).json()["job_id"]
    with c.stream("GET", f"/v1/jobs/{job_id}/stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes()).decode()
    assert "event: ready" in body
    assert "event: end" in body


def test_validation_rejects_zero_duration():
    c = _client([])
    r = c.post("/v1/jobs", json={
        "source_url": "s3://x", "instruction": "cut",
        "duration_s": 0.0,
    })
    assert r.status_code == 422


def test_stub_chat_handles_simple_cut_intent():
    """Without a FakeChatClient injected, the api falls back to a
    deterministic stub. 'cut first 5 seconds' should yield Cut(0,5)."""
    from videoagent.api import build_app as _b
    c = TestClient(_b())  # no chat → stub
    r = c.post("/v1/jobs", json={
        "source_url": "s3://x",
        "instruction": "cut the first 5 seconds",
        "duration_s": 60.0,
    })
    detail = c.get(f"/v1/jobs/{r.json()['job_id']}").json()
    assert detail["status"] == "ready"
    assert detail["plan"]["ops"][0]["op"] == "cut"
    assert detail["plan"]["ops"][0]["end_s"] == 5.0
