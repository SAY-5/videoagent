from __future__ import annotations

from videoagent.batch import BatchJob, run_batch, summarize


def _job(i: int) -> BatchJob:
    return BatchJob(job_id=f"j{i}", input_path=f"/in/{i}.mp4", edit_request="trim")


def test_runs_all_jobs_to_completion() -> None:
    jobs = [_job(i) for i in range(4)]
    results = run_batch(jobs, lambda j: f"/out/{j.job_id}.mp4", max_workers=2)
    assert len(results) == 4
    assert all(r.succeeded for r in results)
    assert results[0].output == "/out/j0.mp4"


def test_failures_are_captured_not_raised() -> None:
    jobs = [_job(0), _job(1)]

    def fn(j: BatchJob) -> str:
        if j.job_id == "j1":
            raise RuntimeError("ffmpeg failed")
        return f"/out/{j.job_id}.mp4"

    results = run_batch(jobs, fn)
    assert results[0].succeeded
    assert not results[1].succeeded
    assert "ffmpeg failed" in results[1].error


def test_empty_input_returns_empty() -> None:
    results = run_batch([], lambda j: "")
    assert results == []


def test_summarize_computes_counts_and_median() -> None:
    jobs = [_job(i) for i in range(5)]
    results = run_batch(jobs, lambda j: f"/out/{j.job_id}.mp4")
    summary = summarize(results)
    assert summary["total"] == 5
    assert summary["succeeded"] == 5
    assert summary["failed"] == 0


def test_summarize_handles_empty() -> None:
    assert summarize([])["total"] == 0


def test_results_in_input_order() -> None:
    jobs = [_job(i) for i in range(10)]
    results = run_batch(jobs, lambda j: j.job_id, max_workers=4)
    assert [r.job_id for r in results] == [f"j{i}" for i in range(10)]
