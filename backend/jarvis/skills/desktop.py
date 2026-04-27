"""Screen capture, vision-friendly bitmaps, and input automation (Windows).

Coordinates are always **normalized 0–1000** (origin top-left of the full
virtual screen grab) so the model does not need pixel values per resolution.

Owner-only: gated from the orchestrator. Requires a VLM in Ollama, e.g.:
``ollama pull qwen2.5vl:7b``  (``qwen3-coder`` is *text-only*, no image input).
"""
from __future__ import annotations

import base64
import io
import logging
import re
import sys
import time
from dataclasses import dataclass

from .base import SkillResult

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"

# Last capture geometry for mapping 0–1000 → absolute pixels
_last_left = 0
_last_top = 0
_last_w = 1
_last_h = 1


@dataclass
class ScreenGrab:
    """Metadata for a capture used with normalized coordinates."""
    left: int
    top: int
    width: int
    height: int
    base64_png: str


def _norm_to_pixel(x: int, y: int) -> tuple[int, int]:
    x = max(0, min(1000, int(x)))
    y = max(0, min(1000, int(y)))
    px = _last_left + (x / 1000.0) * _last_w
    py = _last_top + (y / 1000.0) * _last_h
    return int(round(px)), int(round(py))


def capture_screen(max_width: int = 1280) -> SkillResult:
    """Grab the full virtual screen, downscale for the VLM, return base64 PNG.

    On failure, still returns a SkillResult with success False so the model
    can report the error to the user.
    """
    global _last_left, _last_top, _last_w, _last_h
    if not IS_WIN:
        return SkillResult("Screen capture is only supported on Windows.",
                           intent="desktop", success=False)
    try:
        import mss
        from PIL import Image
    except ImportError as e:
        return SkillResult(
            f"Screen capture needs `mss` and Pillow. {e}",
            intent="desktop", success=False,
        )
    try:
        with mss.mss() as sct:
            mon = sct.monitors[0]
            shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        _last_left, _last_top = int(mon["left"]), int(mon["top"])
        # Map 0–1000 to the *full virtual screen* size (what the VLM's image
        # represents, after uniform downscale the layout is identical).
        _last_w = int(shot.size[0])
        _last_h = int(shot.size[1])
        w, h = img.size
        if w > max_width and max_width > 0:
            nh = int(h * (max_width / w))
            img = img.resize((max_width, nh), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return SkillResult(
            b64,
            intent="desktop_screenshot",
            success=True,
        )
    except Exception as e:
        log.exception("capture_screen failed: %s", e)
        return SkillResult(
            f"Screen capture failed: {e}", intent="desktop", success=False,
        )


def get_last_grab_for_prompt() -> str:
    return (
        f"Virtual screen: offset ({_last_left}, {_last_top}) "
        f"size {_last_w}×{_last_h}px. Coordinates: 0–1000, top-left 0,0 of image."
    )


def click_at(x: int, y: int, double: bool = False) -> SkillResult:
    if not IS_WIN:
        return SkillResult("Clicks are Windows-only.", intent="desktop", success=False)
    try:
        import pyautogui
    except ImportError as e:
        return SkillResult(f"Install pyautogui. {e}", intent="desktop", success=False)
    pyautogui.FAILSAFE = False
    try:
        px, py = _norm_to_pixel(x, y)
        pyautogui.moveTo(px, py, duration=0.12)
        if double:
            pyautogui.doubleClick()
        else:
            pyautogui.click()
        time.sleep(0.08)
        return SkillResult(
            f"Clicked at normalized ({x}, {y}) → screen ({px}, {py}).",
            intent="desktop", success=True,
        )
    except Exception as e:
        log.warning("click_at failed: %s", e)
        return SkillResult(f"Click failed: {e}", intent="desktop", success=False)


def move_pointer(x: int, y: int) -> SkillResult:
    if not IS_WIN:
        return SkillResult("Input is Windows-only.", intent="desktop", success=False)
    try:
        import pyautogui
    except ImportError as e:
        return SkillResult(f"Install pyautogui. {e}", intent="desktop", success=False)
    pyautogui.FAILSAFE = False
    try:
        px, py = _norm_to_pixel(x, y)
        pyautogui.moveTo(px, py, duration=0.15)
        return SkillResult(
            f"Moved to ({x}, {y}) → ({px}, {py}).", intent="desktop", success=True,
        )
    except Exception as e:
        return SkillResult(f"Move failed: {e}", intent="desktop", success=False)


def type_text(text: str) -> SkillResult:
    if not text:
        return SkillResult("Nothing to type.", intent="desktop", success=False)
    if not IS_WIN:
        return SkillResult("Input is Windows-only.", intent="desktop", success=False)
    # Prefer `keyboard` (already a Jarvis dep): handles Unicode on Windows.
    try:
        import keyboard

        time.sleep(0.05)
        keyboard.write(text)
        return SkillResult(
            f"Typed {len(text)} character(s).", intent="desktop", success=True,
        )
    except Exception as e:
        try:
            import pyautogui
            pyautogui.FAILSAFE = False
            time.sleep(0.05)
            pyautogui.write(text, interval=0.02)
            return SkillResult("Typed (ASCII).", intent="desktop", success=True)
        except Exception as e2:
            return SkillResult(
                f"Type failed: {e} / {e2}", intent="desktop", success=False,
            )


def _split_hotkey(s: str) -> list[str]:
    s = s.strip().lower()
    s = s.replace(" ", "+")
    parts: list[str] = []
    for p in s.split("+"):
        p = p.strip()
        if not p:
            continue
        m = p
        if m in ("control", "ctl"):
            m = "ctrl"
        if m in ("return", "cr"):
            m = "enter"
        if m == "win" or m == "windows":
            m = "win"
        if m in ("esc", "escape"):
            m = "esc"
        parts.append(m)
    return parts


def key_combo(keys: str) -> SkillResult:
    if not (keys and keys.strip()):
        return SkillResult("Need a key combination.", intent="desktop", success=False)
    if not IS_WIN:
        return SkillResult("Input is Windows-only.", intent="desktop", success=False)
    try:
        import pyautogui
    except ImportError as e:
        return SkillResult(f"Install pyautogui. {e}", intent="desktop", success=False)
    pyautogui.FAILSAFE = False
    try:
        parts = _split_hotkey(keys)
        if not parts:
            return SkillResult("Invalid keys.", intent="desktop", success=False)
        pyautogui.hotkey(*parts)
        time.sleep(0.05)
        return SkillResult(f"Key combo: {'+'.join(parts)}", intent="desktop", success=True)
    except Exception as e:
        return SkillResult(f"Key combo failed: {e}", intent="desktop", success=False)


def scroll_screen(clicks: int) -> SkillResult:
    if not IS_WIN:
        return SkillResult("Input is Windows-only.", intent="desktop", success=False)
    try:
        import pyautogui
    except ImportError as e:
        return SkillResult(f"Install pyautogui. {e}", intent="desktop", success=False)
    pyautogui.FAILSAFE = False
    try:
        c = int(clicks)
        pyautogui.scroll(c)
        time.sleep(0.06)
        return SkillResult(f"Scrolled {c} click(s).", intent="desktop", success=True)
    except Exception as e:
        return SkillResult(f"Scroll failed: {e}", intent="desktop", success=False)


def scroll_at(x: int, y: int, clicks: int) -> SkillResult:
    """Move the cursor to (x,y) in 0–1000 space, then scroll the wheel.
    Scrolling is applied to whatever is under the cursor (list, page, text).
    Use this to scroll inside a specific pane instead of the global ``scroll``."""
    if not IS_WIN:
        return SkillResult("Input is Windows-only.", intent="desktop", success=False)
    try:
        import pyautogui
    except ImportError as e:
        return SkillResult(f"Install pyautogui. {e}", intent="desktop", success=False)
    pyautogui.FAILSAFE = False
    try:
        c = int(clicks)
        px, py = _norm_to_pixel(x, y)
        pyautogui.moveTo(px, py, duration=0.1)
        pyautogui.scroll(c)
        time.sleep(0.08)
        return SkillResult(
            f"Scrolled {c} at norm ({x},{y}) → ({px},{py}).",
            intent="desktop", success=True,
        )
    except Exception as e:
        return SkillResult(f"scroll_at failed: {e}", intent="desktop", success=False)


_JSON_CALL = re.compile(
    r'^\s*(\{[^{}]*"tool"[\s\S]*\})\s*$',
    re.DOTALL,
)


def run_desktop_action(name: str, args: dict) -> SkillResult:
    """Dispatch a VLM / tool name to the right handler."""
    n = (name or "").strip().lower()
    a = args or {}
    if n in ("click_at", "click"):
        return click_at(
            int(a.get("x", 0)),
            int(a.get("y", 0)),
            bool(a.get("double", False) or a.get("double_click", False)),
        )
    if n in ("move_pointer", "move_mouse", "move"):
        return move_pointer(int(a.get("x", 0)), int(a.get("y", 0)))
    if n in ("type_text", "type"):
        return type_text((a.get("text") or a.get("string") or "") or "")
    if n in ("key_combo", "hotkey", "key"):
        return key_combo((a.get("keys") or a.get("key") or a.get("combo") or "") or "")
    if n in ("scroll", "scroll_screen"):
        return scroll_screen(int(a.get("clicks", a.get("amount", 0) or 0)))
    if n in ("scroll_at", "scroll_in"):
        return scroll_at(
            int(a.get("x", 500)),
            int(a.get("y", 500)),
            int(a.get("clicks", a.get("amount", 0) or 0)),
        )
    if n in ("done", "finish", "complete"):
        msg = (a.get("message") or a.get("summary") or "Done.").strip()
        return SkillResult(msg or "Done.", intent="desktop", success=True)
    return SkillResult(
        f"Unknown desktop action {n!r}.", intent="desktop", success=False,
    )


def parse_fallback_tool_json(s: str) -> tuple[str, dict] | None:
    """If the VLM returns JSON instead of native tool calls, accept it."""
    import json

    s = s.strip()
    if not s:
        return None
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        o = json.loads(s)
    except Exception:
        m = _JSON_CALL.search(s)
        if m:
            try:
                o = json.loads(m.group(1))
            except Exception:
                return None
        else:
            return None
    if not isinstance(o, dict):
        return None
    name = o.get("tool") or o.get("name")
    if not name:
        return None
    args: dict = {}
    for k, v in o.items():
        if k in ("tool", "name"):
            continue
        args[k] = v
    return str(name), args
