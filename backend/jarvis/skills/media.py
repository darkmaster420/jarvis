"""Media transport skills via OS media keys."""
from __future__ import annotations

import logging

from .base import SkillResult

log = logging.getLogger(__name__)


def _send(key: str) -> bool:
    try:
        import keyboard

        keyboard.send(key)
        return True
    except Exception as e:
        log.warning("media key '%s' failed: %s", key, e)
        return False


def play_pause() -> SkillResult:
    ok = _send("play/pause media")
    return SkillResult("Toggled playback." if ok else "Couldn't send media key.",
                       intent="media_play_pause", success=ok)


def next_track() -> SkillResult:
    ok = _send("next track")
    return SkillResult("Next track." if ok else "Couldn't send media key.",
                       intent="media_next", success=ok)


def prev_track() -> SkillResult:
    ok = _send("previous track")
    return SkillResult("Previous track." if ok else "Couldn't send media key.",
                       intent="media_prev", success=ok)


def stop() -> SkillResult:
    ok = _send("stop media")
    return SkillResult("Stopped." if ok else "Couldn't send media key.",
                       intent="media_stop", success=ok)
