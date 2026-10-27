"""Output schema and deterministic validation for resume-match results.

The LLM is probabilistic; this module is not. Anything the model returns must
pass ``MatchResult`` before it is ever shown to a caller.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

MIN_SCORE = 0
MAX_SCORE = 100


class MatchResult(BaseModel):
    """Validated evaluation result. Unknown fields and loose types are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    match_score: Annotated[int, Field(ge=MIN_SCORE, le=MAX_SCORE)]
    top_strengths: list[StrictStr]
    missing_skills: list[StrictStr]
    summary: StrictStr

    @field_validator("top_strengths", "missing_skills")
    @classmethod
    def _items_non_empty(cls, items: list[str]) -> list[str]:
        """Reject blank list entries; keep the model's wording otherwise."""
        if any(not item.strip() for item in items):
            raise ValueError("list items must be non-empty strings")
        return items

    @field_validator("summary")
    @classmethod
    def _summary_two_lines(cls, value: str) -> str:
        """Require exactly two non-empty lines separated by a single newline."""
        lines = value.strip().replace("\r\n", "\n").split("\n")
        if len(lines) != 2 or any(not line.strip() for line in lines):
            raise ValueError("summary must contain exactly two non-empty lines")
        return "\n".join(line.strip() for line in lines)


def validate_payload(payload: Any) -> MatchResult:
    """Validate an already-parsed JSON value. Raises ``pydantic.ValidationError``."""
    return MatchResult.model_validate(payload)


# JSON schema sent to Gemini (structured output). It is a request-side hint;
# ``MatchResult`` above remains the source of truth for acceptance.
GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "match_score": {
            "type": "INTEGER",
            "minimum": MIN_SCORE,
            "maximum": MAX_SCORE,
            "description": "Calibrated 0-100 fit score using the rubric.",
        },
        "top_strengths": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Relevant strengths with explicit evidence in the resume.",
        },
        "missing_skills": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Job requirements not demonstrated in the resume.",
        },
        "summary": {
            "type": "STRING",
            "description": "Exactly two non-empty lines separated by one newline.",
        },
    },
    "required": ["match_score", "top_strengths", "missing_skills", "summary"],
}
