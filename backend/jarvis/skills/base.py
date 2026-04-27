"""Common types for skills."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SkillResult:
    reply: str
    intent: str = ""
    success: bool = True
