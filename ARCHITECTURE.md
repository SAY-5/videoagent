# VideoAgent — Architecture

> A natural-language video editor. The user types "cut the first 10
> seconds and add a fade transition at 1:30" and the system figures
> out which FFmpeg operations to run, executes them, and streams the
> result back to a timeline UI.

## What this project is about

The interesting work isn't the LLM call. The model is surprisingly
good at intent. The hard problem is making the output reliable
enough to actually run against real video files — defining tool
schemas tightly enough that the model **cannot produce a technically
valid JSON output that would still break FFmpeg**.

Constraint design > prompt design. v1 ships:

1. A closed set of FFmpeg-op schemas (Pydantic, with min/max bounds
   on every numeric field).
2. A `Verifier` that probes the input video and rejects ops whose
   timecodes / resolutions / streams don't fit the source. The
   verifier runs *before* FFmpeg, catching hallucinations the
   schema can't.
3. An eval harness that runs a fixed set of edit instructions
   against reference videos and checks frame-level expected
   results. The harness is the feedback loop that surfaced the
   "valid-looking but wrong timecode" failure mode.

## Layout

```
videoagent/
├── videoagent/
│   ├── ops.py              # Pydantic op schemas (closed set)
│   ├── planner.py          # OpenAI function-calling planner
│   ├── verifier.py         # probe + validate-against-source
│   ├── eval.py             # frame-level eval harness
│   ├── ffmpeg_run.py       # wraps `ffmpeg -filter_complex …`
│   └── api.py              # FastAPI: submit, status, result
├── pipeline/               # Go service
│   ├── cmd/pipelined/      # job runner daemon
│   ├── internal/queue/     # in-memory + Postgres queue iface
│   ├── internal/ffmpeg/    # subprocess management
│   └── internal/upload/    # S3 (or local-disk fake)
├── web/                    # React timeline UI
├── eval/                   # reference videos + expected results
└── tests/
```

## The closed op set

Eight verbs cover ~95% of editorial edits we benchmark against.
Each is a Pydantic BaseModel with constrained fields:

```python
class Cut(BaseModel):
    op: Literal["cut"]
    start_s: NonNegativeFloat        # ≥ 0
    end_s:   PositiveFloat           # > start_s, validator-checked
    # Verifier additionally ensures end_s <= source.duration_s.

class Trim(BaseModel):
    op: Literal["trim"]
    keep_start_s: NonNegativeFloat
    keep_end_s:   PositiveFloat

class Concat(BaseModel):
    op: Literal["concat"]
    inputs: list[str]                # min_length=2

class FadeIn(BaseModel):
    op: Literal["fade_in"]
    at_s:       NonNegativeFloat
    duration_s: float = Field(gt=0, le=10)

class FadeOut(BaseModel):
    op: Literal["fade_out"]
    at_s:       NonNegativeFloat
    duration_s: float = Field(gt=0, le=10)

class Speed(BaseModel):
    op: Literal["speed"]
    factor: float = Field(gt=0, le=8)  # > 0, < 8x is FFmpeg-safe

class Volume(BaseModel):
    op: Literal["volume"]
    factor: float = Field(ge=0, le=4)  # 0.0=mute, 4.0=+12dB cap

class Resize(BaseModel):
    op: Literal["resize"]
    width:  int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
```

Pydantic catches type errors and out-of-range values at parse time.
That's not enough — the model can still produce a `Cut(start_s=5,
end_s=200)` against a 120-second video. The Verifier catches that.

## Verifier

After Pydantic accepts an op list, `Verifier.check(plan, source)`
probes the source via `ffprobe` and runs op-by-op invariants:

- `Cut.end_s` ≤ source duration
- `Cut.start_s` < `Cut.end_s`
- `Trim` preserves at least 100ms of usable footage
- `FadeIn.at_s` + `duration_s` ≤ source duration
- `Resize.width`/`height` is divisible by 2 (h264 requirement)
- `Speed.factor` × source duration < 6 hours (sanity)
- `Volume` requires the source to have an audio stream

Every reject returns a structured `VerifyError(op_idx, code, hint)`
the planner can feed back to the LLM for one bounded retry — "the
cut went past the end of the video; the source is 120.4s long".

## LLM planner

OpenAI Chat Completions with `tools=[<each op as a function>]`.
The planner wraps a single `complete()` call:

1. Probe the source. Inject duration, stream summary, and resolution
   into the system message so the model sees what it's editing.
2. Run the `complete()` call with the eight tools as functions. The
   model returns `tool_calls`, each one a `<op>(<args>)`.
3. Parse each call into the matching Pydantic op.
4. Hand the list to the Verifier.
5. On verify failure: re-prompt once with the structured error + a
   directive to fix it. If it still fails, return `PlanFailed` with
   the error chain.

The tool schemas in OpenAI's format are generated directly from the
Pydantic models via `model_json_schema()` — single source of truth
for the constraints.

## Eval harness

`eval/` holds a curated set of `(instruction, source_video,
expected_frame_md5)` tuples. The harness:

1. For each case: planner generates the op list.
2. FFmpeg executes against the source.
3. Sample N frames at fixed timestamps from the output.
4. Compare each frame's MD5 to the expected.
5. Aggregate pass/fail with the diff for failed cases.

The harness is what catches the "hallucinated valid-looking
timecode" failure: the LLM might emit `Cut(end_s=10.5)` instead of
`Cut(end_s=10.0)`, both pass the verifier, both run, but only one
matches the expected frame at 10.0s.

## Pipeline (Go)

The Python planner is the front of house; the Go pipeline is the
kitchen. It owns:

- A job queue (in-memory FIFO for v1; Postgres `SELECT … FOR UPDATE
  SKIP LOCKED` for v2). Jobs carry `{source_url, op_plan, output_key,
  webhook_url}`.
- FFmpeg subprocess management with stdout/stderr streamed to a
  ring buffer for the status endpoint.
- S3 upload (or local-disk fake) of the output, with a presigned
  GET URL for the timeline UI to consume.
- A status endpoint the API polls.

Why Go for the pipeline and Python for the planner: the LLM call is
I/O bound and benefits from Pydantic's validators. FFmpeg subprocess
management is process-management bound and benefits from goroutines
+ channels for the chunked stdout fanout.

## API surface

```
POST /v1/jobs              {source_url, instruction}
                           → 202 {job_id, status: "queued"}
GET  /v1/jobs/{id}         status: queued | planning | running | done | failed
                           plus plan, ffmpeg_log_tail, output_url
GET  /v1/jobs/{id}/stream  SSE: planner events + ffmpeg progress
POST /v1/eval/run          run the harness, return pass/fail per case
GET  /metrics
```

## Performance targets

- Plan end-to-end (probe → LLM → verify): p50 < 2s with mock LLM,
  p50 < 4s with OpenAI gpt-4o-mini.
- FFmpeg execution: source-bound; the harness reports per-case
  wall-time so regressions are observable.
- Eval harness: must complete the curated 12-case suite in < 60s
  on a 2-core CI runner so it runs on every PR.

## Non-goals

- **Audio editing beyond simple volume**: pitch correction,
  ducking, fades on individual tracks. Out of scope; the op set is
  constrained on purpose.
- **Multi-source edits beyond `concat`**: PiP, crossfades, layered
  compositions. The schema-design lesson holds: every additional
  op is a new failure surface that the eval harness needs to cover.
- **Real-time playback**: the timeline UI polls status; live
  scrubbing during edits is a v3 thought, not v1.
