"""The closed set of FFmpeg operations the planner is allowed to emit.

Every op is a Pydantic model with constrained fields. The model's
JSON schema becomes the OpenAI function schema directly, so:

  - the LLM CANNOT choose an op outside this set (function calling
    enforces the closed enum),
  - the LLM CANNOT emit a value outside the bounds (Pydantic
    validates at parse time),
  - the Verifier catches the remaining hallucinations that depend on
    runtime context (e.g. a Cut past the source's actual duration).

This is the v1 thesis: constraint design > prompt design.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# --- ops --------------------------------------------------------------

class Cut(BaseModel):
    """Remove [start_s, end_s) from the source. The output skips that
    range."""
    op: Literal["cut"] = "cut"
    start_s: float = Field(ge=0.0, description="Start time in seconds (inclusive).")
    end_s:   float = Field(gt=0.0, description="End time in seconds (exclusive).")

    @model_validator(mode="after")
    def _start_before_end(self) -> Cut:
        if self.end_s <= self.start_s:
            raise ValueError(f"end_s ({self.end_s}) must be > start_s ({self.start_s})")
        return self


class Trim(BaseModel):
    """Keep [keep_start_s, keep_end_s); drop everything else."""
    op: Literal["trim"] = "trim"
    keep_start_s: float = Field(ge=0.0)
    keep_end_s:   float = Field(gt=0.0)

    @model_validator(mode="after")
    def _start_before_end(self) -> Trim:
        if self.keep_end_s <= self.keep_start_s:
            raise ValueError(
                f"keep_end_s ({self.keep_end_s}) must be > keep_start_s ({self.keep_start_s})"
            )
        return self


class Concat(BaseModel):
    """Concatenate the source with `inputs` (additional source URLs/
    paths). The current source becomes input 0."""
    op: Literal["concat"] = "concat"
    inputs: list[str] = Field(min_length=1, max_length=8)


class FadeIn(BaseModel):
    """Apply a fade-in starting at `at_s` lasting `duration_s`."""
    op: Literal["fade_in"] = "fade_in"
    at_s:       float = Field(ge=0.0)
    duration_s: float = Field(gt=0.0, le=10.0)


class FadeOut(BaseModel):
    """Apply a fade-out starting at `at_s` lasting `duration_s`."""
    op: Literal["fade_out"] = "fade_out"
    at_s:       float = Field(ge=0.0)
    duration_s: float = Field(gt=0.0, le=10.0)


class Speed(BaseModel):
    """Change playback speed. factor=2.0 doubles speed; 0.5 halves."""
    op: Literal["speed"] = "speed"
    factor: float = Field(gt=0.0, le=8.0,
                          description=">0 and ≤8x — FFmpeg's safe atempo range")


class Volume(BaseModel):
    """Multiply audio by `factor`. 0=mute, 1=unchanged, 4=+12dB cap."""
    op: Literal["volume"] = "volume"
    factor: float = Field(ge=0.0, le=4.0)


class Resize(BaseModel):
    """Scale to width x height. Both must be even (h264 requirement)."""
    op: Literal["resize"] = "resize"
    width:  int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)

    @model_validator(mode="after")
    def _even_dims(self) -> Resize:
        if self.width % 2 or self.height % 2:
            raise ValueError("width and height must be even (h264 codec requirement)")
        return self


# --- discriminated union --------------------------------------------

# The planner returns a list of these. Pydantic's discriminated-union
# parsing routes each dict to the right model based on the `op` field;
# OpenAI's tool-call mechanism enforces the same one-of constraint.
Op = Annotated[
    Cut | Trim | Concat | FadeIn | FadeOut | Speed | Volume | Resize,
    Field(discriminator="op"),
]


class Plan(BaseModel):
    ops: list[Op] = Field(default_factory=list, max_length=32)


# Listing for documentation + tool-schema generation.
ALL_OPS: list[type[BaseModel]] = [
    Cut, Trim, Concat, FadeIn, FadeOut, Speed, Volume, Resize,
]


def op_descriptions() -> dict[str, str]:
    """Map each op name to its docstring summary, for help / debugging."""
    return {
        cls.model_fields["op"].default: (cls.__doc__ or "").strip().splitlines()[0]
        for cls in ALL_OPS
    }
