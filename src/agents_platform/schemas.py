"""Small shared contract for specialist results."""

from typing import Literal

from pydantic import BaseModel


class SpecialistResult(BaseModel):
    summary: str
    observations: list[str]
    next_action: Literal["wait", "complete", "needs_human"]
    reason: str
    proposed_action: str | None
