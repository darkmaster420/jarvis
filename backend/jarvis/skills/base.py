"""Common types for skills."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SkillResult:
    reply: str
    intent: str = ""
    success: bool = True
    data: dict[str, Any] | None = None
