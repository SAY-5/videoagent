"""v4: batch processing with parallel job runner.

The single-video path (v1-v3) fits the interactive case. v4 adds
a batch runner: take a list of (input_path, edit_request) jobs,
run them in a bounded worker pool, return a per-job result.

We deliberately don't fan out to subprocess.Popen here — that
would couple the batch runner to a specific ffmpeg-invocation
strategy. The runner takes a `process_one` callable so users
plug in their own (the test path uses a deterministic stub).
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BatchJob:
    job_id: str
    input_path: str
    edit_request: str


@dataclass(frozen=True, slots=True)
class BatchResult:
    job_id: str
    succeeded: bool
    duration_ms: float
    output: str = ""
    error: str = ""


ProcessOne = Callable[[BatchJob], str]


def run_batch(jobs: list[BatchJob], process_one: ProcessOne,
              max_workers: int = 4) -> list[BatchResult]:
    """Process `jobs` in a thread pool. Each `process_one(job)`
    returns the output path on success or raises on failure.

    Returns one BatchResult per job, in the same order as input.
    """

    results: list[BatchResult] = [None] * len(jobs)  # type: ignore[list-item]

    def _one(idx: int) -> tuple[int, BatchResult]:
        job = jobs[idx]
        start = time.perf_counter()
        try:
            output = process_one(job)
            return idx, BatchResult(
                job_id=job.job_id, succeeded=True,
                duration_ms=(time.perf_counter() - start) * 1e3,
                output=output,
            )
        except Exception as e:
            return idx, BatchResult(
                job_id=job.job_id, succeeded=False,
                duration_ms=(time.perf_counter() - start) * 1e3,
                error=str(e),
            )

    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for idx, r in pool.map(_one, range(len(jobs))):
            results[idx] = r
    return results


def summarize(results: list[BatchResult]) -> dict:
    """One-shot summary: success rate + median duration. Used by
    the orchestrator to decide whether the batch run succeeded
    overall."""
    n = len(results)
    if n == 0:
        return {"total": 0, "succeeded": 0, "failed": 0, "median_ms": 0.0}
    durations = sorted(r.duration_ms for r in results)
    median = durations[n // 2]
    return {
        "total": n,
        "succeeded": sum(1 for r in results if r.succeeded),
        "failed": sum(1 for r in results if not r.succeeded),
        "median_ms": median,
    }
