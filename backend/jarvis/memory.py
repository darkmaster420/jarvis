"""Long-term memory for Jarvis.

Stage-1 "learning" is entirely data-driven: the LLM writes to these JSON
files via tool calls, and the orchestrator injects relevant entries back
into its system prompt on every turn. Nothing here touches executable
code, so the worst case for bad memory writes is a confused Jarvis, not a
bricked install. Delete the memory directory to reset.

Files (under <repo>/memory/):
  - facts.json        list of {"text": str, "tags": [str], "ts": float}
  - aliases.json      dict of {phrase -> target}   (e.g. "work" -> "cursor.exe")
  - routines.json     dict of {name  -> [step, ...]} each step is a natural
                      language command that gets re-run through Jarvis
  - preferences.json  dict of {key -> value}        simple k/v store
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}


class Memory:
    """Thread-safe JSON-backed memory store."""

    FACTS_NAME       = "facts.json"
    ALIASES_NAME     = "aliases.json"
    ROUTINES_NAME    = "routines.json"
    PREFERENCES_NAME = "preferences.json"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.facts: list[dict[str, Any]]       = self._load(self.FACTS_NAME, default=[])
        self.aliases: dict[str, str]           = self._load(self.ALIASES_NAME, default={})
        self.routines: dict[str, list[str]]    = self._load(self.ROUTINES_NAME, default={})
        self.preferences: dict[str, Any]       = self._load(self.PREFERENCES_NAME, default={})

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.root / name

    def _load(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if not p.exists():
            return default if not isinstance(default, (list, dict)) \
                else type(default)()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("memory: failed to load %s (%s); starting empty", name, e)
            return default if not isinstance(default, (list, dict)) \
                else type(default)()
        # Be forgiving about shape drift.
        if isinstance(default, list)  and not isinstance(data, list):  return []
        if isinstance(default, dict)  and not isinstance(data, dict):  return {}
        return data

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------
    def remember(self, text: str, tags: list[str] | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty fact")
        with self._lock:
            entry = {
                "text": text,
                "tags": sorted({t.lower().strip() for t in (tags or []) if t}),
                "ts":   time.time(),
            }
            # dedupe by exact text match
            self.facts = [f for f in self.facts if f.get("text") != text]
            self.facts.append(entry)
            _atomic_write(self._path(self.FACTS_NAME), self.facts)
            return entry

    def forget(self, pattern: str) -> int:
        """Delete facts whose text OR any tag matches `pattern` (substring, ci)."""
        needle = (pattern or "").strip().lower()
        if not needle:
            return 0
        with self._lock:
            before = len(self.facts)
            self.facts = [
                f for f in self.facts
                if needle not in (f.get("text") or "").lower()
                and needle not in " ".join(f.get("tags") or []).lower()
            ]
            removed = before - len(self.facts)
            if removed:
                _atomic_write(self._path(self.FACTS_NAME), self.facts)
            return removed

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        """Return up to `limit` facts most relevant to `query`.
        Scoring = token overlap with text+tags, newer ties break older."""
        q_tokens = _tokens(query or "")
        if not q_tokens:
            return []
        with self._lock:
            scored: list[tuple[int, float, dict]] = []
            for f in self.facts:
                hay = _tokens((f.get("text") or "")) \
                    | _tokens(" ".join(f.get("tags") or []))
                score = len(q_tokens & hay)
                if score > 0:
                    scored.append((score, f.get("ts", 0.0), f))
            scored.sort(key=lambda t: (-t[0], -t[1]))
            return [f for _, _, f in scored[:limit]]

    def all_facts(self) -> list[dict]:
        with self._lock:
            return list(self.facts)

    # ------------------------------------------------------------------
    # aliases  (used by skills.system.open_app for personalised launches)
    # ------------------------------------------------------------------
    def add_alias(self, phrase: str, target: str) -> None:
        phrase = (phrase or "").strip().lower()
        target = (target or "").strip()
        if not phrase or not target:
            raise ValueError("phrase and target are both required")
        with self._lock:
            self.aliases[phrase] = target
            _atomic_write(self._path(self.ALIASES_NAME), self.aliases)

    def remove_alias(self, phrase: str) -> bool:
        key = (phrase or "").strip().lower()
        with self._lock:
            if key in self.aliases:
                del self.aliases[key]
                _atomic_write(self._path(self.ALIASES_NAME), self.aliases)
                return True
            return False

    def get_alias(self, phrase: str) -> str | None:
        key = (phrase or "").strip().lower()
        with self._lock:
            return self.aliases.get(key)

    def list_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self.aliases)

    # ------------------------------------------------------------------
    # routines  (named multi-step macros)
    # ------------------------------------------------------------------
    def define_routine(self, name: str, steps: list[str]) -> None:
        name = (name or "").strip().lower()
        if not name or not steps:
            raise ValueError("routine needs a name and at least one step")
        clean = [s.strip() for s in steps if s and s.strip()]
        if not clean:
            raise ValueError("routine has no valid steps")
        with self._lock:
            self.routines[name] = clean
            _atomic_write(self._path(self.ROUTINES_NAME), self.routines)

    def remove_routine(self, name: str) -> bool:
        key = (name or "").strip().lower()
        with self._lock:
            if key in self.routines:
                del self.routines[key]
                _atomic_write(self._path(self.ROUTINES_NAME), self.routines)
                return True
            return False

    def get_routine(self, name: str) -> list[str] | None:
        with self._lock:
            return list(self.routines.get((name or "").strip().lower()) or []) or None

    def list_routines(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self.routines.items()}

    # ------------------------------------------------------------------
    # preferences  (flat k/v)
    # ------------------------------------------------------------------
    def set_preference(self, key: str, value: Any) -> None:
        k = (key or "").strip().lower()
        if not k:
            raise ValueError("empty preference key")
        with self._lock:
            self.preferences[k] = value
            _atomic_write(self._path(self.PREFERENCES_NAME), self.preferences)

    def get_preference(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.preferences.get((key or "").strip().lower(), default)

    # ------------------------------------------------------------------
    # context injection helpers
    # ------------------------------------------------------------------
    def context_block(self, query: str) -> str:
        """Build a short system-prompt section with the memories relevant to
        `query` plus any always-on facts tagged 'pinned'. Returns '' when
        there's nothing to inject."""
        lines: list[str] = []
        with self._lock:
            pinned = [f for f in self.facts if "pinned" in (f.get("tags") or [])]
        for f in pinned[:5]:
            lines.append(f"- {f['text']}")
        already = {f["text"] for f in pinned}
        for f in self.recall(query, limit=5):
            if f["text"] in already:
                continue
            lines.append(f"- {f['text']}")
        if not lines:
            return ""
        return (
            "Known facts about the user (use to personalise your answer):\n"
            + "\n".join(lines)
        )
