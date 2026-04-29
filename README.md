# VideoAgent

A natural-language video editor. The user types

> cut the first 10 seconds and add a fade transition at 1:30

and the system figures out which FFmpeg operations to run, executes
them, and streams the result to a timeline UI.

## What's actually interesting

The LLM call isn't the hard part. The hard part is making the
output reliable enough to actually run against real video files —
defining the tool schemas tightly enough that the model **cannot
produce a technically valid JSON output that would still break
FFmpeg**.

Three layers do the work:

1. **Closed-set Pydantic op schemas** (`videoagent/ops.py`) —
   eight verbs (Cut, Trim, Concat, FadeIn, FadeOut, Speed, Volume,
   Resize) with min/max bounds on every numeric field. The OpenAI
   function schema is generated directly from these, so the model
   physically cannot call an op outside the set or pass an out-of-
   range value.
2. **Source-aware verifier** (`videoagent/verifier.py`) — runs after
   parsing, before FFmpeg. Catches the runtime-context failures
   Pydantic can't: a `Cut(end_s=200)` that's structurally valid
   but past a 120-second source. Every reject returns a structured
   `VerifyError(op_idx, code, hint)` the planner feeds back to the
   LLM for one bounded retry.
3. **Frame-level eval harness** (`videoagent/eval.py`) — a fixed
   set of `(instruction, source, expected_plan_or_frame_md5)`
   tuples. This is the feedback loop that surfaced the one failure
   schemas + verifier missed: hallucinated valid-looking timecodes
   that pass both layers but produce the wrong edit.

> "The hardest problem was not getting the LLM to understand the
> edit intent — it is surprisingly good at that. The hardest
> problem was defining the tool schemas tightly enough that the
> model could not produce a technically valid JSON output that
> would still break FFmpeg. Constraint design matters more than
> prompt design for reliability."

## Stack

- **Python** for the LLM planner + verifier + eval harness.
  Pydantic v2, FastAPI, OpenAI function calling.
- **Go** for the pipeline. In-memory job queue (`SELECT … FOR
  UPDATE SKIP LOCKED`-shaped interface for the production swap),
  FFmpeg subprocess management with stderr ring-buffered for
  status, S3 (or local-disk fake) upload of the output.
- **React-ish vanilla JS** timeline UI. SVG ruler + clip strips,
  ops overlay, inspector panel.

## Quick start

```bash
pip install -e ".[dev,openai]"
uvicorn videoagent.api:app --reload --port 8000
# In another terminal — the Go pipeline:
cd pipeline && go run ./cmd/pipelined -addr :8090
# UI:
python -m http.server 5173 --directory web
```

Without `OPENAI_API_KEY`, the API falls back to a deterministic
stub that handles the common patterns (`cut first N seconds`,
`fade in at T`, `fade out at T`) so the demo runs offline.

## Tests

```bash
pytest -q                    # 59 Python tests
cd pipeline && go test ./... # 18 Go tests
```

- `test_ops.py` (15) — every constraint Pydantic enforces.
- `test_verifier.py` (10) — source-aware checks (cut past end,
  trim too short, fade past end, volume without audio, speed
  yielding too-long output).
- `test_planner.py` (8) — tool-call → Plan, verify failure feeds
  structured error into the retry, replan budget capped, unknown
  tool handled, empty response handled.
- `test_eval.py` (6) — plan-equality with float tolerance, plan
  inequality flagged, multi-case suite, JSON round-trip.
- `test_api.py` (7) — submit/poll, unverifiable-plan failed
  status, stream emits terminal event, validation rejects bad
  inputs, stub handles simple intent.
- `test_ffmpeg_run.py` (8) — argv shape per op (mirrored on the
  Go side to catch language drift).
- Go: `internal/ffmpeg` (8) — same argv shape; `internal/queue`
  (4) — FIFO + blocking pop + cancellation; `internal/upload`
  (3) — local copy + nested dirs + empty-key reject; +1 results
  store unit.

## Companion projects

Part of the SAY-5 portfolio under [github.com/SAY-5](https://github.com/SAY-5).

## License

MIT.
