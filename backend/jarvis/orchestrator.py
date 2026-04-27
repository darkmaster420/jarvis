"""Intent router: fast-path regex for simple commands, LLM with tool-calling
for open-ended phrases and chat."""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .config import LlmCfg, PermissionsCfg
from .memory import Memory
from .skills import desktop, media, system, web
from .skills import info as info_skill
from .skills.base import SkillResult

log = logging.getLogger(__name__)

RESTRICTED_DENIED = SkillResult(
    "Sorry, you're not authorised for that.", intent="denied", success=False
)

# Injected into system / tool instructions so the model can author
# ``user_skills/skills.py`` correctly (bundle + sandbox). Keep in sync with
# ``jarvis.user_skills`` (ALLOWED_IMPORTS / validation).
USER_SKILL_BUNDLE_GUIDE = """
USER SKILL BUNDLE — authoring ``user_skills/skills.py`` (via propose_patch)

What this is: One Python file defines your custom tools. After the human
approves the patch in the HUD, Jarvis hot-reloads tools (no full restart).

Big picture:
- The main Jarvis model sees each tool as a function whose name, description,
  and JSON Schema come from the SKILLS dict.
- When it calls a tool, your ``handle_<name>`` runs with the arguments as a
  flat dict; you return text + success for the user.

1) SKILLS dict (required)
- Type: ``dict[str, dict]`` assigned to the name ``SKILLS`` at module level.
- Each KEY is the tool name exposed to the LLM. Rules: snake_case, 3-32 chars,
  starts with ``a-z``, only ``[a-z0-9_]``. The key MUST match the handler:
  tool ``weather_now`` needs ``def handle_weather_now(args: dict) -> dict:``.
- Each VALUE must include:
  - ``description`` (string): Clear, specific text the model uses to decide
    when to call this tool. Say what it does, what it does NOT do, and how
    arguments are meant to be used (units, format, examples).
  - ``parameters`` (dict): JSON Schema for the tool call. Use
    ``{"type": "object", "properties": {...}, "required": [...]}``.
    Give every argument a ``type`` (``string``, ``number``, ``integer``,
    ``boolean``, …) and a short ``description``. List required keys in
    ``required``. Optional args: omit from ``required`` and give defaults
    inside ``handle_*`` when missing.

2) Handler functions (required)
- For each SKILLS key ``my_tool``, define ``def handle_my_tool(args: dict) -> dict:``.
- ``args`` only contains keys from ``parameters.properties`` (may be empty).
- Always return a dict with BOTH keys:
  - ``reply``: short user-facing string (what Jarvis should say or show).
  - ``success``: boolean (False if validation failed or the action could not
    complete; still explain in ``reply``).
- Keep handler logic aligned with the schema: validate types, clamp ranges,
  return helpful errors in ``reply`` instead of raising when possible.

3) propose_patch rules for this file
- ``target`` must identify the bundle. Use the virtual path ``user_skills/skills.py``
  **or** the **absolute path** to ``skills.py`` on this machine (forward slashes
  are fine on Windows). The server resolves both to the same file under your
  Jarvis data directory. A concrete path for *this* PC is injected in the system
  prompt block ``USER SKILLS — FILE LOCATION`` when available — copy that string
  if you are unsure.
- ``new_content`` must be the ENTIRE file (complete valid Python from first
  line to last). Never send a unified diff or a fragment.
- You must base the file on the latest full ``skills.py`` text you have in
  this conversation. If you do not have it, ask the user to paste the file or
  open it in their editor and share it — do not silently drop tools you cannot
  see. Only ship a from-scratch file if the user clearly wants to replace
  everything and accepts losing prior tools.
- When ADDING a tool: copy forward every existing SKILLS entry and every
  existing ``handle_*``; append the new pair. Do not drop unrelated tools.
- When EDITING: update SKILLS and the matching ``handle_*`` together so
  descriptions, schema, and code stay consistent.
- When REMOVING: delete the SKILLS entry and its ``handle_*`` function.

4) Sandbox (enforced before load — code is rejected if violated)
- Allowed ``import`` roots only: json, re, math, random, datetime, time,
  statistics, string, textwrap, collections, itertools, functools, operator,
  dataclasses, typing, fractions, decimal, calendar, enum, webbrowser,
  urllib.
- Disallowed patterns include: ``exec``, ``eval``, ``open``, subprocess,
  raw sockets (except what urllib allows), ``getattr``/``setattr``, and other
  dynamic/bypass tricks — the loader scans the AST.
- User skills must NOT drive keyboard, mouse, or generic window automation.
  Use built-ins: open_app, close_app, open_url, close_browser_tab, etc.

5) If you cannot fulfill the request safely
- Say so in plain language; do not emit bundle code that breaks the sandbox.
""".strip()


def _tool_narration_message(
    tool_calls: list[tuple[str, dict]],
) -> str | None:
    """Short line for TTS + HUD while tools run (called from a worker thread)."""
    names = {n for n, _ in tool_calls}
    if (
        ("propose_patch" in names or "create_user_skill" in names)
        and "web_search" in names
    ):
        return (
            "Let me look that up online, then update your skills. "
            "This may take a little while."
        )
    if "propose_patch" in names:
        return (
            "I do not have a built-in for that. Updating your skills file—"
            "this may take a moment."
        )
    if "create_user_skill" in names:
        return (
            "I do not have a built-in for that. Creating a new skill—"
            "this may take a moment."
        )
    if "web_search" in names:
        return "Looking that up on the web."
    return None

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": (
                "Open a Windows application, document, or system page, or a "
                "known website. Accepts names such as 'file explorer', "
                "'notepad', 'bluetooth settings', 'calculator', 'browser', "
                "'YouTube', 'Google', 'GitHub'. On Windows, sites and https "
                "URIs are opened in the default browser; always call this tool "
                "for those requests (do not claim you have no browser)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "App name or alias (e.g. 'file explorer').",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": (
                "Terminate a running Windows application by name. Use this "
                "when the user says 'close', 'quit', 'kill', 'stop', or "
                "'exit' an app. Accepts the same informal names as open_app "
                "('chrome', 'steam', 'discord', 'file explorer', etc.). Do "
                "NOT use for closing a single browser tab — that kills the "
                "whole process; use close_browser_tab instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "App name or alias to close.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_browser_tab",
            "description": (
                "Close the active tab in the frontmost app by sending Ctrl+W "
                "(standard in Chrome, Edge, Firefox, and most browsers). Use "
                "when the user asks to close a tab, the current tab, or 'this' "
                "tab — not the whole browser. The browser or target window "
                "should already be focused."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reopen_closed_browser_tab",
            "description": (
                "Reopen the last closed tab in the focused browser (Ctrl+Shift+T), "
                "if supported by the app."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Run a Google search in the default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": (
                "Open a URL in the system default browser (e.g. youtube.com, "
                "https://...). Use for bare domains; prefer open_app for "
                "YouTube/Chrome/named sites if unsure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "A URL or bare domain (e.g. 'github.com').",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_stats",
            "description": (
                "Read current CPU load, RAM usage, and battery (if any) from "
                "this PC via psutil. Use when the user asks for PC/computer/"
                "system status, performance, resource usage, or how hard the "
                "machine is working. Do not refuse these questions — call this "
                "tool instead of guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a durable fact about the user or their environment so "
                "future sessions can personalise replies. Use this whenever "
                "the user shares a preference, a name, a project, or a "
                "recurring detail worth keeping. Keep facts one sentence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The single fact to store."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short keyword tags (e.g. ['name', 'preference']). Use 'pinned' for facts that should always be in context.",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete saved facts whose text or tags contain the given pattern (case-insensitive substring).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_alias",
            "description": (
                "Teach Jarvis a personal launch shortcut. When the user later "
                "says `phrase`, open_app will launch `target` (an exe name, "
                "a URI like steam:// or ms-settings:*, or a full path)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["phrase", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "define_routine",
            "description": (
                "Save a named multi-step routine. Each step is a plain-English "
                "command that Jarvis will re-run through its normal pipeline "
                "when the routine is invoked (e.g. steps=['open spotify', "
                "'set volume to 30', 'open cursor'])."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_routine",
            "description": "Execute a previously defined routine by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_user_skill",
            "description": (
                "Legacy: add a skill as a separate ``*.py`` file. Prefer editing "
                "the single bundle with propose_patch (target "
                "``user_skills/skills.py``, full file): after HUD approval, tools "
                "hot-reload. Use this only for a one-off separate module. "
                "Sandbox: allowlisted stdlib only (json, re, math, time, "
                "webbrowser, urllib, collections, typing, …); no subprocess, "
                "open, eval. No keyboard/window control — use close_browser_tab "
                "for browser tabs. Module MUST have a docstring, PARAMETERS "
                "(JSON schema), and ``def handle(args: dict) -> dict`` returning "
                "keys 'reply' and 'success'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "snake_case module name (3-32 chars, a-z0-9_).",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-sentence summary of what the skill does.",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Full Python source for a standalone module "
                            "(docstring, PARAMETERS, handle). Prefer "
                            "propose_patch on user_skills/skills.py instead; "
                            "see USER SKILL BUNDLE guide."
                        ),
                    },
                },
                "required": ["name", "description", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_user_skill",
            "description": (
                "Delete a legacy per-file user skill (``name.py``). Tools defined "
                "only in ``user_skills/skills.py`` must be removed by editing that "
                "bundle via propose_patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_patch",
            "description": (
                "Propose replacing a whole file. You MUST send the FULL new "
                "file text (not a diff). The HUD reviews it; it applies only "
                "after approval. "
                "Use ``user_skills/skills.py`` for custom tools (SKILLS dict + "
                "handle_<name> per tool; follow USER SKILL BUNDLE guide in "
                "instructions; hot-reloads). "
                "Use ``backend/jarvis/...`` only for core fixes (restart Jarvis "
                "after approval)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "For the skills bundle: ``user_skills/skills.py`` "
                            "or the absolute on-disk path to ``skills.py`` (same "
                            "file; see USER SKILLS — FILE LOCATION in instructions). "
                            "For core code: ``backend/jarvis/<path>.py`` under the "
                            "install/repo."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "One sentence for the human reviewer: what changed "
                            "and why (e.g. 'Add tool X for …', 'Fix schema for Y')."
                        ),
                    },
                    "new_content": {
                        "type": "string",
                        "description": (
                            "Complete new file contents, byte-ready. For "
                            "user_skills/skills.py: entire bundle (module docstring, "
                            "SKILLS, all handle_* functions) per USER SKILL BUNDLE "
                            "guide — never truncate or omit unrelated tools."
                        ),
                    },
                },
                "required": ["target", "description", "new_content"],
            },
        },
    },
]

# Ollama tool specs for the *vision* model only (screen + mouse/keyboard).
# Invoked from ``_run_desktop_vision``; not mixed into the main text LLM.
DESKTOP_VISION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": "Left-click (or double-click) at x,y. Use 0-1000.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "double": {
                        "type": "boolean",
                        "description": "If true, double-click instead of single click.",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_pointer",
            "description": "Move the mouse to x,y (0-1000) without clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type Unicode text into the focused field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_combo",
            "description": "Press a key chord, e.g. ctrl+s, alt+f4, win+r.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Mouse wheel. Positive=scroll up, negative=down (typical 3-6).",
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks": {"type": "integer"},
                },
                "required": ["clicks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "The user's goal is complete; return a one-sentence summary for voice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
]

_DESKTOP_VISION_PAT = re.compile(
    r"\b("
    r"click|double-?\s*click|"
    r"look at (the )?screen|"
    r"whats? on (the|my) screen|"
    r"what(?:'s| is) on (the|my) screen|"
    r"see (the|my) screen|"
    r"on my screen|"
    r"in this window|"
    r"move (the )?mouse|"
    r"scroll (up|down|the)|"
    r"type (this|it|in )|"
    r"press (ctrl|alt|shift|win|windows)\b|"
    r"key combo|"
    r"my screen\?"
    r")\b",
    re.I,
)

_CREATE_SKILL_PAT = re.compile(
    r"(?:"
    # make/create/... a (new) (jarvis) skill | custom tool
    r"\b(?:create|make|build|write|add|author)\s+"
    r"(?:me\s+)?(?:a\s+)?(?:new\s+)?"
    r"(?:(?:jarvis\s+)?(?:skill|skills)\b|custom\s+tool\b)"
    r"|"
    r"\b(?:create|make|build|write|add|author)\s+"
    r"(?:me\s+)?(?:a\s+)?(?:new\s+)?custom\s+tool\s+(?:that|to|for)\b"
    r"|"
    # skill(s) that/to/for/which ...
    r"\b(?:new\s+)?(?:user\s+)?(?:skill|skills)\s+(?:that|to|for|which)\b"
    r"|"
    r"\b(?:new\s+)?(?:user\s+)?skill\s+for\b"
    r"|"
    # I want / need a skill
    r"\b(?:i\s+)?(?:want|need|would\s+like)\s+"
    r"(?:a\s+)?(?:new\s+)?(?:jarvis\s+)?(?:skill|skills)\b"
    r"|"
    # can you make ... skill / custom tool
    r"\b(?:can|could)\s+you\s+(?:please\s+)?"
    r"(?:make|create|add|build|write)\s+"
    r"(?:me\s+)?(?:a\s+)?(?:new\s+)?"
    r"(?:(?:jarvis\s+)?(?:skill|skills)|custom\s+tool)\b"
    r"|"
    # define / design / implement a skill
    r"\b(?:define|design|implement|extend)\s+"
    r"(?:a\s+)?(?:new\s+)?(?:user\s+)?(?:skill|skills)\b"
    r"|"
    # add a tool to Jarvis
    r"\b(?:add|create)\s+(?:a\s+)?(?:new\s+)?(?:custom\s+)?tool\s+to\s+"
    r"(?:jarvis|my\s+assistant)\b"
    r"|"
    # teach Jarvis a skill
    r"\bteach\s+(?:jarvis\s+)?(?:a\s+)?(?:new\s+)?(?:skill|skills)\b"
    r")",
    re.I,
)


def _wants_desktop_vision(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 4:
        return False
    return bool(_DESKTOP_VISION_PAT.search(t))


def _wants_user_skill(text: str) -> bool:
    return bool(_CREATE_SKILL_PAT.search(text or ""))


def _skill_authoring_tool_calls_ok(
    tool_calls: list[tuple[str, dict]],
) -> bool:
    """True if every tool call is skill authoring (no mixed hallucinated names)."""
    if not tool_calls:
        return False
    return all(
        name in ("propose_patch", "create_user_skill")
        for name, _ in tool_calls
    )


def _skill_authoring_invented_tool_names(
    tool_calls: list[tuple[str, dict]],
) -> list[str]:
    return sorted({
        n for n, _ in (tool_calls or [])
        if n not in ("propose_patch", "create_user_skill")
    })


def _skill_authoring_native_nudge(
    tool_calls: list[tuple[str, dict]],
    attempt: int,
) -> str:
    bad = _skill_authoring_invented_tool_names(tool_calls)
    if not bad:
        tool_phrase = "Invented tool names are not available on this host"
    elif len(bad) == 1:
        tool_phrase = f"{bad[0]!r} is not a real tool on this host"
    else:
        tool_phrase = (
            f"{', '.join(repr(n) for n in bad)} are not real tools on this host"
        )
    msg = (
        "\n\n[SKILL AUTHORING — You must call the tool propose_patch (full-file "
        "replace of user_skills/skills.py) or create_user_skill only. "
        f"{tool_phrase}. Use only propose_patch or create_user_skill in tool_calls. "
        "Implement the user request as a new SKILLS entry + handle_<name> inside "
        "the bundle text from the system message; copy that entire file into "
        "new_content, edit it, then call propose_patch.]"
    )
    if attempt >= 2:
        msg += (
            "\n[Final warning: any tool name other than propose_patch or "
            "create_user_skill will be rejected. Use propose_patch with "
            "target, description, and new_content (complete Python file).]"
        )
    return msg


def _skill_authoring_prompt_nudge(pname: str, attempt: int) -> str:
    msg = (
        "\n\n[SKILL AUTHORING — Your JSON must use "
        '"tool": "propose_patch" (or "create_user_skill") only. '
        f"You used {pname!r}, which is not a valid tool. "
        "Emit propose_patch with the full updated skills.py from the system "
        "message as new_content.]"
    )
    if attempt >= 2:
        msg += (
            '\n[Last retry: one line, {"tool":"propose_patch","args":{'
            '"target":"user_skills/skills.py","description":"…",'
            '"new_content":"…full file…"}}]'
        )
    return msg


def _skill_authoring_exhausted_reply() -> str:
    return (
        "I should add that as a skill in your user_skills bundle via "
        "propose_patch, but the model kept calling a made-up tool name instead "
        "of propose_patch. Please try again in one short sentence, or open the "
        "patch panel and approve if a patch was already proposed."
    )


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s%]", " ", text.lower()).strip()


def _openai_choice_message(resp: dict) -> dict | None:
    ch = (resp.get("choices") or [])
    if not ch or not isinstance(ch[0], dict):
        return None
    m = ch[0].get("message")
    return m if isinstance(m, dict) else None


def _openai_finish_reason(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    ch = (resp.get("choices") or [])
    if not ch or not isinstance(ch[0], dict):
        return ""
    r = ch[0].get("finish_reason")
    return str(r or "").strip().lower()


def _skill_authoring_tool_specs(all_specs: list[dict]) -> list[dict]:
    keep = {"propose_patch", "create_user_skill"}
    out: list[dict] = []
    for spec in all_specs:
        fn = spec.get("function", {}) if isinstance(spec, dict) else {}
        name = fn.get("name")
        if name in keep:
            out.append(spec)
    return out or all_specs


def _extract_tool_calls(resp: Any) -> list[tuple[str, dict]]:
    """Extract (name, args) from an Ollama chat response or OpenAI-compatible JSON."""
    msg: Any = None
    if isinstance(resp, dict) and "choices" in resp:
        msg = _openai_choice_message(resp)
    else:
        msg = getattr(resp, "message", None)
        if msg is None and isinstance(resp, dict):
            msg = resp.get("message")
    if msg is None:
        return []
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls is None and isinstance(msg, dict):
        tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return []
    out: list[tuple[str, dict]] = []
    for tc in tool_calls:
        fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
        if fn is None:
            continue
        name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
        raw_args = getattr(fn, "arguments", None)
        if raw_args is None and isinstance(fn, dict):
            raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        if name:
            out.append((name, args))
    return out


_XML_FN_RE    = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.DOTALL)
_XML_PARAM_RE = re.compile(r"<parameter=([\w.-]+)>(.*?)</parameter>", re.DOTALL)


def _parse_xml_tool_calls(content: str) -> list[tuple[str, dict]]:
    """Parse coder-style tool calls of the form
       <function=NAME><parameter=KEY>VALUE</parameter>...</function>.
    Integer/float/bool values are coerced; everything else is kept as str."""
    out: list[tuple[str, dict]] = []
    for m in _XML_FN_RE.finditer(content):
        name = m.group(1).strip()
        body = m.group(2)
        args: dict[str, Any] = {}
        for pm in _XML_PARAM_RE.finditer(body):
            key = pm.group(1).strip()
            raw = pm.group(2).strip()
            if raw.lower() in ("true", "false"):
                args[key] = raw.lower() == "true"
                continue
            try:
                args[key] = int(raw)
                continue
            except ValueError:
                pass
            try:
                args[key] = float(raw)
                continue
            except ValueError:
                pass
            # JSON arrays/objects inside a parameter block.
            if raw.startswith(("[", "{")):
                try:
                    args[key] = json.loads(raw)
                    continue
                except Exception:
                    pass
            args[key] = raw
        if name:
            out.append((name, args))
    return out


def _extract_content(resp: Any) -> str:
    msg: Any = None
    if isinstance(resp, dict) and "choices" in resp:
        msg = _openai_choice_message(resp)
    else:
        msg = getattr(resp, "message", None)
        if msg is None and isinstance(resp, dict):
            msg = resp.get("message")
    if msg is None:
        return ""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
        return " ".join(parts).strip()
    return (content or "").strip() if isinstance(content, str) else ""


class Orchestrator:
    def __init__(self, llm_cfg: LlmCfg, perms: PermissionsCfg,
                 memory: Memory | None = None,
                 user_skills: Any = None,
                 patches: Any = None):
        self.llm_cfg = llm_cfg
        self.perms = perms
        self.memory = memory
        self.user_skills_mgr = user_skills
        self.patches = patches
        if memory is not None:
            # Let open_app consult user-taught aliases.
            system.set_alias_lookup(memory.get_alias)
        self._history: deque[tuple[str, str]] = deque(maxlen=10)
        self._tool_support: dict[str, bool] = {}
        self._ollama = None
        prov = (llm_cfg.provider or "ollama").lower().replace("-", "_")
        if prov in ("lmstudio", "openai_compatible"):
            prov = "lm_studio"
        self._llm_provider = prov
        # Runtime-registered user skills (Stage 2). name -> (spec, callable)
        self._user_skills: dict[str, Callable[[dict], SkillResult]] = {}
        self._user_tool_specs: list[dict] = []
        if self._llm_provider == "ollama":
            try:
                import ollama

                self._ollama = ollama.Client(host=llm_cfg.host)
            except Exception as e:  # pragma: no cover
                log.warning("ollama client unavailable: %s", e)

        # Website / browser fast paths: models sometimes refuse to "use a
        # browser" instead of calling tools; these run on normalized text (see
        # handle()) before the LLM.
        _web_site = (
            "youtube|you tube|yt|google|gmail|reddit|twitch|netflix|"
            "spotify|twitter|github|discord"
        )
        _web_browsers = "browser|edge|chrome|firefox|brave|microsoft edge|google chrome"
        self._rules: list[tuple[re.Pattern[str], Callable[[re.Match[str], str], SkillResult], str]] = [
            (re.compile(
                rf"(?i)\b(?:open|launch|start|visit|show)\s+"
                rf"(?:the|my|a|your)\s+(?P<w>{_web_site})\b"
            ),
             lambda m, u: system.open_app(m.group("w").lower()),
             "open_app"),
            (re.compile(
                rf"(?i)\b(?:open|launch|start|visit|show)\s+(?P<w>{_web_site})\b"
            ),
             lambda m, u: system.open_app(m.group("w").lower()),
             "open_app"),
            (re.compile(
                rf"(?i)\bgo\s+to\s+(?:the\s+)?(?P<w>{_web_site})\b"
            ),
             lambda m, u: system.open_app(m.group("w").lower()),
             "open_app"),
            (re.compile(
                rf"(?i)\b(?:open|launch|start|visit)\s+"
                rf"(?:(?:the|my|your|a)\s+)?(?P<w>{_web_browsers})\b"
            ),
             lambda m, u: system.open_app(m.group("w").lower()),
             "open_app"),
            # "youtube com" with dots stripped by _norm
            (re.compile(
                r"(?i)\b(?:open|launch|visit|go to|show)\s+"
                r"(?:the\s+)?(?:www\s+)?youtube\s+com\b"
            ),
             lambda m, u: web.open_url("https://www.youtube.com"),
             "open_url"),
            (re.compile(
                r"(?i)\b(?:open|launch|visit|go to|show)\s+"
                r"(?:the\s+)?(?:www\s+)?google\s+com\b"
            ),
             lambda m, u: web.open_url("https://www.google.com"),
             "open_url"),
            (re.compile(
                r"(?i)\b(?:open|launch|visit|go to|show)\s+"
                r"(?:the\s+)?(?:www\s+)?github\s+com\b"
            ),
             lambda m, u: web.open_url("https://github.com"),
             "open_url"),
            (re.compile(r"^(what.?s|tell me|whats)?\s*the?\s*time\b"),
             lambda m, u: info_skill.time_now(), "time"),
            (re.compile(r"^(what.?s|tell me|whats)?\s*the?\s*date\b|what day"),
             lambda m, u: info_skill.date_today(), "date"),
            (re.compile(r"\b(weather|forecast)\b(?:\s+in\s+(?P<loc>.+))?"),
             lambda m, u: info_skill.weather(m.group("loc") or ""), "weather"),
            (re.compile(
                r"(?i)\b("
                r"(system|cpu|processor|memory|ram|battery)\s*(stats|status|usage)?"
                r"|(?:the\s+)?(pc|computer|machine|hardware)\s+"
                r"(stats|status|usage|health|diagnostics?)"
                r"|resource\s+usage\b"
                r")\b"
            ),
             lambda m, u: info_skill.system_stats(), "sys_stats"),

            (re.compile(r"\b(mute)\b"),
             lambda m, u: system.volume("mute"), "volume"),
            (re.compile(r"\b(unmute)\b"),
             lambda m, u: system.volume("unmute"), "volume"),
            (re.compile(r"\b(volume|sound)\s+up\b|louder\b|turn it up"),
             lambda m, u: system.volume("up"), "volume"),
            (re.compile(r"\b(volume|sound)\s+down\b|quieter\b|turn it down"),
             lambda m, u: system.volume("down"), "volume"),
            (re.compile(r"\b(set\s+)?volume\s+(to\s+)?(?P<v>\d{1,3})\s*(%|percent)?"),
             lambda m, u: system.volume("set", min(1.0, int(m.group("v")) / 100)),
             "volume"),

            (re.compile(r"\b(play|pause|resume)\b(?!\s+\w)"),
             lambda m, u: media.play_pause(), "media_play_pause"),
            (re.compile(r"\b(next|skip)\s+(track|song)\b|\bnext\b"),
             lambda m, u: media.next_track(), "media_next"),
            (re.compile(r"\b(previous|prev|last)\s+(track|song)\b"),
             lambda m, u: media.prev_track(), "media_prev"),
            (re.compile(r"\bstop\s+(music|playback|media)\b"),
             lambda m, u: media.stop(), "media_stop"),

            (re.compile(
                r"(?i)(?:^|\b)(?:please\s+)?(?:close|kill)\s+"
                r"(?:(?:this|the|current|that|a|my)\s+)*"
                r"(?:(?:www\s+)?youtube(?:\s+com)?|you(?:\s*)tube|yt)\b"
            ),
             lambda m, u: web.close_browser_tab(), "close_browser_tab"),
            (re.compile(
                r"(?i)(?:^|\b)(?:please\s+)?(?:close|kill)\s+"
                r"(?:(?:this|the|current|that|a|my)\s+)*"
                r"(?:browser\s+)?tab(?:s)?\b"
            ),
             lambda m, u: web.close_browser_tab(), "close_browser_tab"),
            (re.compile(
                r"(?i)\b(?:re)?open\s+last\s+closed\s+tab\b|"
                r"\brestore\s+(?:the\s+)?last\s+tab\b"
            ),
             lambda m, u: web.reopen_closed_browser_tab(), "reopen_closed_browser_tab"),
            (re.compile(
                r"^(?:please\s+)?(?:close|quit|exit|kill|terminate)\s+"
                r"(?:the\s+|my\s+)?"
                r"(?P<app>(?!pc\b|computer\b|workstation\b|screen\b|"
                r"window\b|windows\b|everything\b)"
                r".+?)"
                r"\s*(?:app|application|process|program)?\s*$"),
             lambda m, u: system.close_app(m.group("app").strip()),
             "close_app"),

            (re.compile(r"\block\s+(the\s+)?(pc|computer|workstation|screen)\b|lock it"),
             lambda m, u: system.lock(), "lock"),
            (re.compile(r"\b(go to )?sleep\b|\bsuspend\b"),
             lambda m, u: system.sleep_pc(), "sleep"),
            (re.compile(r"\bshut\s*down\b|power off|turn off (the )?(pc|computer)"),
             lambda m, u: system.shutdown(), "shutdown"),
            (re.compile(r"\bcancel\s+shutdown\b|abort shutdown"),
             lambda m, u: system.cancel_shutdown(), "cancel_shutdown"),
        ]

    def _authorised(self, intent: str, user: str) -> bool:
        if intent not in self.perms.restricted_intents:
            return True
        owner = (self.perms.owner or "").lower()
        return bool(owner) and user.lower() == owner

    def _llm_ready(self) -> bool:
        if self._llm_provider == "lm_studio":
            return bool((self.llm_cfg.openai_base_url or "").strip())
        return self._ollama is not None

    def _lm_unreachable_msg(self) -> str:
        if self._llm_provider == "lm_studio":
            return (
                "I could not reach the LM Studio API. Open LM Studio, load a model, "
                "turn on the local server (Developer → Start Server), and ensure "
                f"the URL matches llm.openai_base_url in config "
                f"(default {self.llm_cfg.openai_base_url})."
            )
        return "I couldn't reach the language model. Is Ollama running?"

    def _lm_native_chat(
        self,
        *,
        model: str,
        messages: list,
        tools: list | None,
        temperature: float,
        max_tokens: int | None = None,
    ) -> Any:
        if self._llm_provider == "ollama":
            opts: dict[str, Any] = {"temperature": temperature}
            if max_tokens is not None:
                opts["num_predict"] = max_tokens
            kw: dict[str, Any] = dict(model=model, messages=messages, options=opts)
            if tools:
                kw["tools"] = tools
            if self._ollama is None:
                raise RuntimeError("Ollama client is not available")
            return self._ollama.chat(**kw)
        from .openai_compat import chat_completions

        mt = max_tokens
        if mt is None:
            mt = getattr(self.llm_cfg, "openai_max_tokens", None)
        return chat_completions(
            self.llm_cfg.openai_base_url,
            self.llm_cfg.openai_api_key,
            model,
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=mt,
        )

    def _unload_ollama_model(self, model: str) -> None:
        """Best-effort unload to free RAM/VRAM before switching models."""
        model = (model or "").strip()
        if not model or self._ollama is None or self._llm_provider != "ollama":
            return
        try:
            # Ollama's documented unload path is generate/chat with
            # keep_alive=0. The empty prompt avoids doing actual work.
            self._ollama.generate(model=model, prompt="", keep_alive=0)
            log.info("requested Ollama unload for %s", model)
        except Exception as e:
            log.debug("Ollama unload ignored for %s: %s", model, e)

    def _run_desktop_vision(self, text: str, user: str) -> SkillResult:
        """Multi-round loop: capture screen → VLM (with image) → tool calls
        (click, type, …) → repeat. Uses ``llm.vision_model``, not the main
        text-only coder model.
        """
        if not self._llm_ready():
            return SkillResult(
                self._lm_unreachable_msg(),
                intent="desktop", success=False,
            )
        primary_vm = (getattr(self.llm_cfg, "vision_model", None) or "").strip()
        if not primary_vm:
            if self._llm_provider == "lm_studio":
                hint = (
                    "Desktop vision is off: set `llm.vision_model` in config to a "
                    "VLM id exactly as listed by LM Studio (GET /v1/models). "
                    "Text-only models cannot see the screen."
                )
            else:
                hint = (
                    "Desktop vision is off: set `llm.vision_model` in config "
                    "to a vision model, e.g. qwen2.5vl:7b, then run: "
                    "ollama pull qwen2.5vl:7b "
                    "(qwen3-coder and other text-only models cannot see the screen.)"
                )
            return SkillResult(hint, intent="desktop", success=False)
        vision_models = [primary_vm]
        for fallback in getattr(self.llm_cfg, "vision_fallback_models", []) or []:
            fb = str(fallback).strip()
            if fb and fb not in vision_models:
                vision_models.append(fb)
        bad_vision_models: set[str] = set()
        # Free the large text model before starting a *different* VLM. If chat
        # and vision use the same multimodal model, keep it warm to avoid slow
        # unload/reload cycles.
        if (self.llm_cfg.model or "").strip() != primary_vm:
            if self._llm_provider == "ollama":
                self._unload_ollama_model(self.llm_cfg.model)
        max_w = int(getattr(self.llm_cfg, "vision_max_screenshot_w", 1280) or 1280)
        max_r = int(getattr(self.llm_cfg, "vision_desktop_rounds", 3) or 3)
        max_r = max(1, min(20, max_r))
        post_s = float(getattr(self.llm_cfg, "vision_post_action_s", 0.18) or 0.18)
        n_pred = int(getattr(self.llm_cfg, "vision_num_predict", 384) or 384)
        vis_opts: dict[str, Any] = {"temperature": 0.0, "num_predict": max(64, n_pred)}

        sys_vision = (
            "You control the user's Windows desktop. Each turn you see ONE full "
            "screenshot of the virtual desktop (all monitors), downscaled; layout is "
            "preserved. Coordinates are integers x,y in 0..1000: (0,0) = top-left of "
            "this image, (1000,1000) = bottom-right. "
            "Do not use markdown. Return exactly one compact JSON object, one action per turn.\n\n"
            "SEARCH AND ACT: Treat every task like finding a target (button, link, text, icon).\n"
            "- If the target is VISIBLE, move/click it (or type) as appropriate.\n"
            "- If the target is NOT visible (below the fold, off-screen, inside a list), "
            "do NOT click randomly. Move the pointer over the right area (e.g. center of "
            "a list, web page, or settings pane) and use scroll or scroll_at with negative "
            "clicks to bring lower content into view (typical: several steps across turns). "
            "If it might be above, use a small positive clicks value. "
            "Use scroll_at(x,y,clicks) to scroll a specific window or panel under (x,y).\n"
            "- After scrolling, the next image updates; only then click the target you find.\n\n"
            "Allowed tools:\n"
            "{\"tool\":\"click_at\",\"x\":500,\"y\":400,\"double\":false}\n"
            "{\"tool\":\"move_pointer\",\"x\":500,\"y\":400}\n"
            "{\"tool\":\"scroll\",\"clicks\":-6}\n"
            "{\"tool\":\"scroll_at\",\"x\":500,\"y\":450,\"clicks\":-8}\n"
            "{\"tool\":\"type_text\",\"text\":\"hello\"}\n"
            "{\"tool\":\"key_combo\",\"keys\":\"ctrl+s\"}\n"
            "{\"tool\":\"done\",\"message\":\"short summary\"}\n"
            "For questions about what is on screen, use done with the answer. "
            "For UI tasks, one physical action per reply; you always get a fresh screenshot next."
        )
        action_log: list[str] = []

        for _round in range(max_r):
            cap = desktop.capture_screen(max_w)
            if not cap.success or not (cap.reply or "").strip():
                return SkillResult(
                    cap.reply or "Could not capture the screen.",
                    intent="desktop", success=False,
                )
            b64 = (cap.reply or "").strip()
            ctx = text
            if action_log:
                ctx = (
                    f"User goal: {text}\n"
                    f"Actions so far: {'; '.join(action_log)}\n"
                    "Here is a fresh screenshot. Continue with tools, or call done."
                )
            messages = [
                {"role": "system", "content": sys_vision},
                {"role": "user", "content": ctx, "images": [b64]},
            ]
            resp = None
            last_err = ""
            for vm in vision_models:
                if vm in bad_vision_models:
                    continue
                try:
                    # Ollama VLMs often reject `tools` with images (HTTP 400). LM
                    # Studio uses the same JSON-in-content protocol here.
                    if self._llm_provider == "lm_studio":
                        from .openai_compat import (
                            chat_completions,
                            ollama_messages_to_openai,
                        )
                        resp = chat_completions(
                            self.llm_cfg.openai_base_url,
                            self.llm_cfg.openai_api_key,
                            vm,
                            ollama_messages_to_openai(messages),
                            tools=None,
                            temperature=float(vis_opts.get("temperature", 0.0)),
                            max_tokens=max(64, n_pred),
                        )
                    else:
                        resp = self._ollama.chat(
                            model=vm,
                            messages=messages,
                            options=vis_opts,
                        )
                    if vm != primary_vm:
                        log.info("vision using fallback model: %s", vm)
                    break
                except Exception as e:
                    last_err = str(e)
                    bad_vision_models.add(vm)
                    log.warning("vision model %s failed: %s", vm, e)
                    # Free anything partially loaded before trying fallback.
                    self._unload_ollama_model(vm)
            if resp is None:
                models = ", ".join(vision_models)
                return SkillResult(
                    f"Vision error: {last_err}. Tried: {models}.",
                    intent="desktop", success=False,
                )

            narration = _extract_content(resp)
            tool_calls: list[tuple[str, dict]] = []
            if "<function=" in narration:
                tool_calls = tool_calls + _parse_xml_tool_calls(narration)
            if narration.strip():
                alt = desktop.parse_fallback_tool_json(narration)
                if alt:
                    tool_calls = [alt]
            if not tool_calls:
                reply = narration.strip() or "I am not sure what to do on screen."
                self._history.append(("user", text))
                self._history.append(("assistant", reply))
                return SkillResult(reply, intent="desktop", success=True)

            for name, args in tool_calls:
                n = (name or "").lower().strip()
                if n in ("done", "finish", "complete"):
                    msg = (
                        (args or {}).get("message")
                        or (args or {}).get("summary")
                        or ""
                    ).strip() or (narration.strip() or "Done.")
                    self._history.append(("user", text))
                    self._history.append(("assistant", msg))
                    return SkillResult(msg, intent="desktop", success=True)
                r = desktop.run_desktop_action(name, args or {})
                action_log.append(f"{name} → {(r.reply or '')[:100]}")
                if post_s > 0:
                    time.sleep(post_s)

        self._history.append(("user", text))
        self._history.append((
            "assistant",
            "Stopped after the maximum number of on-screen steps.",
        ))
        return SkillResult(
            "I stopped after the maximum number of on-screen steps. "
            "Check the display and try again, or rephrase the goal.",
            intent="desktop", success=False,
        )

    def handle(
        self,
        text: str,
        user: str,
        on_status: Callable[[str], None] | None = None,
    ) -> SkillResult:
        text = text.strip()
        if not text:
            return SkillResult("", intent="empty", success=False)
        # _norm() strips ":", so https:// is invisible to the regex list below
        m_url = re.search(r"https?://[^\s<>\)]+", text, re.IGNORECASE)
        if m_url and self._authorised("open_url", user):
            raw = m_url.group(0).rstrip(".,;)]\"'")
            try:
                result = web.open_url(raw)
                self._history.append(("user", text))
                self._history.append(("assistant", result.reply))
                return result
            except Exception as e:
                log.exception("open_url fast path failed for %r", raw)
                return SkillResult(
                    f"Could not open that link: {e}",
                    intent="open_url", success=False,
                )
        if m_url and not self._authorised("open_url", user):
            log.info("denied intent=open_url (raw url) for user=%s", user)
            return RESTRICTED_DENIED
        norm = _norm(text)
        for pat, fn, intent in self._rules:
            m = pat.search(norm)
            if m:
                if not self._authorised(intent, user):
                    log.info("denied intent=%s for user=%s", intent, user)
                    return RESTRICTED_DENIED
                try:
                    result = fn(m, user)
                    self._history.append(("user", text))
                    self._history.append(("assistant", result.reply))
                    return result
                except Exception as e:
                    log.exception("skill '%s' failed", intent)
                    return SkillResult(f"That skill crashed: {e}",
                                       intent=intent, success=False)
        if _wants_desktop_vision(text):
            if not self._authorised("desktop", user):
                log.info("denied intent=desktop for user=%s", user)
                return RESTRICTED_DENIED
            return self._run_desktop_vision(text, user)
        return self._chat(text, user, on_status=on_status)

    def _run_tool(self, name: str, args: dict, user: str) -> SkillResult:
        intent = {
            "open_app":   "open_app",
            "close_app":  "close_app",
            "close_browser_tab": "close_browser_tab",
            "reopen_closed_browser_tab": "reopen_closed_browser_tab",
            "web_search": "web_search",
            "open_url":   "open_url",
            "get_system_stats": "sys_stats",
        }.get(name, name)
        if not self._authorised(intent, user):
            return RESTRICTED_DENIED
        if name == "close_browser_tab":
            return web.close_browser_tab()
        if name == "reopen_closed_browser_tab":
            return web.reopen_closed_browser_tab()
        if name == "open_app":
            target = (args.get("name") or args.get("app") or "").strip()
            return system.open_app(target)
        if name == "close_app":
            target = (args.get("name") or args.get("app") or "").strip()
            return system.close_app(target)
        if name == "web_search":
            query = (args.get("query") or args.get("q") or "").strip()
            return web.search(query)
        if name == "open_url":
            url = (args.get("url") or "").strip()
            return web.open_url(url)
        if name == "get_system_stats":
            return info_skill.system_stats()
        if name == "remember":
            if self.memory is None:
                return SkillResult("Memory isn't configured.",
                                   intent="remember", success=False)
            fact = (args.get("fact") or args.get("text") or "").strip()
            tags = args.get("tags") or []
            if not fact:
                return SkillResult("Remember what?", intent="remember", success=False)
            try:
                self.memory.remember(fact, tags if isinstance(tags, list) else [])
            except Exception as e:
                return SkillResult(f"Couldn't save that: {e}",
                                   intent="remember", success=False)
            return SkillResult(f"Got it, I'll remember that.", intent="remember")
        if name == "forget":
            if self.memory is None:
                return SkillResult("Memory isn't configured.",
                                   intent="forget", success=False)
            pattern = (args.get("pattern") or args.get("query") or "").strip()
            removed = self.memory.forget(pattern) if pattern else 0
            if removed:
                return SkillResult(f"Forgot {removed} fact{'s' if removed != 1 else ''}.",
                                   intent="forget")
            return SkillResult("Nothing matched.", intent="forget", success=False)
        if name == "add_alias":
            if self.memory is None:
                return SkillResult("Memory isn't configured.",
                                   intent="add_alias", success=False)
            phrase = (args.get("phrase") or "").strip()
            target = (args.get("target") or "").strip()
            if not phrase or not target:
                return SkillResult("I need both a phrase and a target.",
                                   intent="add_alias", success=False)
            try:
                self.memory.add_alias(phrase, target)
            except Exception as e:
                return SkillResult(f"Couldn't save alias: {e}",
                                   intent="add_alias", success=False)
            return SkillResult(f"Okay, '{phrase}' will open {target}.",
                               intent="add_alias")
        if name == "define_routine":
            if self.memory is None:
                return SkillResult("Memory isn't configured.",
                                   intent="define_routine", success=False)
            rname = (args.get("name") or "").strip()
            steps = args.get("steps") or []
            if not rname or not isinstance(steps, list) or not steps:
                return SkillResult("Routines need a name and a list of steps.",
                                   intent="define_routine", success=False)
            try:
                self.memory.define_routine(rname, [str(s) for s in steps])
            except Exception as e:
                return SkillResult(f"Couldn't save routine: {e}",
                                   intent="define_routine", success=False)
            return SkillResult(f"Routine '{rname}' saved with {len(steps)} step(s).",
                               intent="define_routine")
        if name == "run_routine":
            if self.memory is None:
                return SkillResult("Memory isn't configured.",
                                   intent="run_routine", success=False)
            rname  = (args.get("name") or "").strip()
            steps  = self.memory.get_routine(rname) if rname else None
            if not steps:
                return SkillResult(f"I don't have a routine called '{rname}'.",
                                   intent="run_routine", success=False)
            replies: list[str] = []
            ok_all = True
            for step in steps:
                log.info("routine %s step: %s", rname, step)
                r = self.handle(step, user)
                if not r.success:
                    ok_all = False
                if r.reply:
                    replies.append(r.reply)
            summary = f"Ran '{rname}': " + "; ".join(replies) if replies \
                else f"Ran '{rname}'."
            return SkillResult(summary, intent="run_routine", success=ok_all)
        if name == "create_user_skill":
            if self.user_skills_mgr is None:
                return SkillResult("User-skill authoring isn't configured.",
                                   intent="create_user_skill", success=False)
            return self.user_skills_mgr.create(
                (args.get("name") or "").strip(),
                (args.get("description") or "").strip(),
                args.get("code") or "",
            )
        if name == "remove_user_skill":
            if self.user_skills_mgr is None:
                return SkillResult("User-skill authoring isn't configured.",
                                   intent="remove_user_skill", success=False)
            return self.user_skills_mgr.remove(
                (args.get("name") or "").strip()
            )
        if name == "propose_patch":
            if self.patches is None:
                return SkillResult("Patch system isn't configured.",
                                   intent="propose_patch", success=False)
            try:
                rec = self.patches.propose(
                    target=(args.get("target") or "").strip(),
                    description=(args.get("description") or "").strip(),
                    new_content=args.get("new_content") or "",
                )
            except Exception as e:
                return SkillResult(f"Patch rejected: {e}",
                                   intent="propose_patch", success=False)
            msg = (
                f"I've proposed a patch to {rec['target']} — please approve it "
                "in the HUD's patch review panel."
            )
            t = rec.get("target") or ""
            if (
                isinstance(t, str)
                and t.startswith("user_skills/")
                and self.user_skills_mgr is not None
            ):
                bundle = (Path(self.user_skills_mgr.dir) / "skills.py").resolve()
                msg = (
                    f"I've proposed a patch to {t} (file on disk: {bundle}) — "
                    "please approve it in the HUD's patch review panel."
                )
            return SkillResult(msg, intent="propose_patch")
        # Dispatch to any user-authored skills registered at runtime.
        if name in self._user_skills:
            try:
                return self._user_skills[name](args)
            except Exception as e:
                log.exception("user skill '%s' crashed", name)
                return SkillResult(f"User skill {name!r} crashed: {e}",
                                   intent=name, success=False)
        log.warning("unknown tool call: %s(%s)", name, args)
        return SkillResult(
            f"I don't know how to do {name!r}.",
            intent="unknown_tool", success=False,
        )

    def _skills_bundle_location_hint(self) -> str:
        """Tell the model the real bundle path and a concrete propose_patch example."""
        if self.user_skills_mgr is None:
            return ""
        bundle = (Path(self.user_skills_mgr.dir) / "skills.py").resolve()
        posix = bundle.as_posix()
        example = (
            '{"tool": "propose_patch", "args": {"target": "'
            + posix
            + '", "description": "Add roll_dice tool", "new_content": "<paste '
            'complete skills.py here>"}}'
        )
        return (
            "\n=== USER SKILLS — FILE LOCATION (this PC) ===\n"
            f"Editable bundle path:\n  {bundle}\n\n"
            "For propose_patch, \"target\" may be user_skills/skills.py or "
            f"this absolute path (forward slashes OK):\n  {posix}\n\n"
            "Example (prompt-style single-line tool JSON):\n"
            f"  {example}\n"
        )

    def _skill_authoring_context(self) -> str:
        """Extra system text + live ``skills.py`` so the model can propose_patch."""
        body = ""
        if self.user_skills_mgr is not None:
            try:
                body = self.user_skills_mgr.read_bundle_text()
            except Exception as e:
                log.debug("read skills bundle failed: %s", e)
                body = ""
        if not (body and body.strip()):
            body = (
                "# (skills.py unavailable — check Jarvis data directory permissions.)\n"
            )
        return (
            "\n\n=== THIS TURN: SKILL AUTHORING ===\n"
            "The user asked to create, add, or change a Jarvis skill. "
            "You MUST NOT call invented tool names (e.g. flip_coin, close_foo) — "
            "those are not in the API. New behavior goes inside the file below via "
            "propose_patch.\n"
            "You MUST use ONLY: propose_patch (target user_skills/skills.py or the "
            "absolute path from USER SKILLS — FILE LOCATION, full updated file) OR "
            "create_user_skill (legacy single-file module).\n"
            "Prefer propose_patch. Start from the ENTIRE file below; merge your "
            "change so every existing SKILLS key and handle_* remains unless you "
            "mean to remove one — never shrink the file to only the new tool.\n\n"
            f"Current user_skills/skills.py:\n```python\n{body}\n```\n"
        )

    def _prompt_tool_instructions(self) -> str:
        specs = TOOLS_SPEC + self._user_tool_specs
        lines = [
            "You have these tools available. Use them when they match the user request:",
        ]
        for spec in specs:
            fn = spec.get("function", {})
            name = fn.get("name", "")
            desc = " ".join(str(fn.get("description", "")).split())
            params = fn.get("parameters", {}).get("properties", {})
            arg_names = ", ".join(params.keys())
            sig = f"{name}({arg_names})" if arg_names else f"{name}()"
            lines.append(f"  {sig} - {desc}")
        hint = self._skills_bundle_location_hint()
        if hint:
            lines.append(hint.rstrip())
        lines.extend([
            "",
            "Tool selection rules:",
            "- To add or change user tools, prefer propose_patch with target user_skills/skills.py and the FULL file: define SKILLS and handle_<name> for each tool; after HUD approval tools hot-reload. If you do not have the current file text in this conversation, ask the user to paste skills.py first so you do not erase existing tools.",
            "- If the user asks you to create, make, build, write, add, or author a new Jarvis skill, use propose_patch on user_skills/skills.py (not create_user_skill) unless they explicitly want a separate legacy module.",
            "- If the user wants something and NO existing tool can do it, add it to the skills bundle (after web_search for facts you do not know). Do not wait for the user to say 'create a skill'.",
            "- Legacy create_user_skill writes a separate *.py file; use only when appropriate. Same sandbox: allowlisted stdlib; PARAMETERS and handle(args); never for keyboard/tab control (use close_browser_tab).",
            "- If a requested skill needs unsafe actions such as subprocess, raw sockets, or arbitrary file I/O, explain the limitation instead of writing unsafe code.",
            "- If the user asks to close/quit/exit/kill/stop an app, use close_app, never open_app.",
            "- If the user asks to close a browser tab (not the whole browser), use close_browser_tab; use reopen_closed_browser_tab to undo.",
            "- If the user asks to open/launch/start an app or URL, use open_app or open_url.",
            "- Never claim you cannot open websites or have no browser; use open_app/open_url and the system default browser.",
            "",
            USER_SKILL_BUNDLE_GUIDE,
            "",
            "If a tool is needed, respond with EXACTLY one JSON object on a single line, no prose:",
            "  {\"tool\": \"<name>\", \"args\": {<args>}}",
            "If no tool is needed, reply in plain natural English (1-2 sentences), with NO JSON.",
        ])
        return "\n".join(lines)

    def _build_messages(
        self,
        text: str,
        user: str,
        tool_style: str,
        *,
        skill_authoring: bool = False,
    ) -> list[dict[str, Any]]:
        sys_prompt = self.llm_cfg.system_prompt
        if tool_style == "prompt":
            sys_prompt = sys_prompt + "\n\n" + self._prompt_tool_instructions()
        elif skill_authoring:
            # Keep skill-authoring native prompts compact to preserve token budget
            # for a large propose_patch(new_content=...) tool call.
            sys_prompt = sys_prompt + (
                "\n"
                "SKILL AUTHORING MODE (native tool call):\n"
                "- Use ONLY propose_patch or create_user_skill.\n"
                "- Prefer propose_patch on user_skills/skills.py (or absolute path).\n"
                "- Do NOT invent tool names.\n"
                "- Return a valid tool call, not prose.\n"
            ) + self._skills_bundle_location_hint()
        else:
            sys_prompt = sys_prompt + (
                "\n"
                "TOOL USE RULES (VERY IMPORTANT):\n"
                "- If the user asks you to open, launch, start, or run ANY "
                "application, window, menu, or system page: you MUST call "
                "open_app. Do NOT just say 'Opening X' - actually invoke the "
                "tool. This includes phrases like 'open bluetooth settings', "
                "'launch calc', 'open explorer', 'start spotify'.\n"
                "- If the user asks you to close, quit, exit, kill, stop, or "
                "terminate a running application (e.g. 'close chrome', 'quit "
                "steam', 'kill discord'): you MUST call close_app with that "
                "name. NEVER call open_app for close/quit/exit requests.\n"
                "- If they ask to close a *tab* (browser tab or 'this tab'), call "
                "close_browser_tab — not close_app (which would kill the whole browser).\n"
                "- If the user asks you to go to, visit, or open a website or "
                "URL: call open_url (or open_app for a named site like YouTube). "
                "The OS default browser is used; never say you have no browser "
                "or cannot open websites on Windows.\n"
                "- If the user asks you to search, google, or look something "
                "up online: call web_search.\n"
                "- CAPABILITY GAP: when nothing above fits but the user still "
                "wants a concrete action, use web_search if you need current "
                "facts, then propose_patch on user_skills/skills.py with the "
                "full updated bundle (SKILLS + handle_<name> functions). Do it "
                "proactively—do not ask the user to say 'create a skill' first, "
                "and do not claim you 'cannot' when you can extend the bundle.\n"
                "- If the user explicitly asks to create, make, build, write, add, "
                "or author a new Jarvis skill: prefer propose_patch target "
                "user_skills/skills.py; use create_user_skill only for a separate "
                "legacy module. Only refuse if the task truly needs subprocess, "
                "raw sockets, or arbitrary file access.\n"
                "- For chit-chat or questions you can answer from knowledge, "
                "reply in plain text (1-2 sentences, no tool).\n"
                "- Never describe what you would do - either do it by calling "
                "a tool, or answer the question.\n\n"
            ) + USER_SKILL_BUNDLE_GUIDE + "\n" + self._skills_bundle_location_hint()
        if skill_authoring:
            sys_prompt = sys_prompt + self._skill_authoring_context()
        if user and user != "guest":
            sys_prompt = sys_prompt + f"\nThe speaker's name is {user.title()}."
        if self.memory is not None:
            mem_block = self.memory.context_block(text)
            if mem_block:
                sys_prompt = sys_prompt + "\n\n" + mem_block
        msgs: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        for role, content in self._history:
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": text})
        return msgs

    def _parse_prompted_tool(self, content: str) -> tuple[str, dict] | None:
        if not content:
            return None
        # Some coder models (notably qwen3-coder) emit tool calls as XML-style
        # <function=NAME><parameter=KEY>VALUE</parameter>...</function> blocks
        # in the assistant content instead of via native tool_calls. Parse
        # that shape first since it tends to be unambiguous.
        if "<function=" in content:
            out = _parse_xml_tool_calls(content)
            if out:
                return out[0]
        stripped = content.strip().strip("`")
        # Allow a ```json block or raw JSON.
        if stripped.startswith("json\n"):
            stripped = stripped[5:]
        brace = stripped.find("{")
        last  = stripped.rfind("}")
        if brace < 0 or last < brace:
            return None
        try:
            obj = json.loads(stripped[brace:last + 1])
        except Exception:
            return None
        name = obj.get("tool") or obj.get("name")
        args = obj.get("args") or obj.get("arguments") or {}
        if not name or not isinstance(args, dict):
            return None
        return name, args

    def _chat(
        self,
        text: str,
        user: str,
        on_status: Callable[[str], None] | None = None,
    ) -> SkillResult:
        if not self._llm_ready():
            return SkillResult(
                "I don't have a language model available right now.",
                intent="chat", success=False,
            )

        model = self.llm_cfg.model
        native_supported = self._tool_support.get(model, None)
        skill_boost = _wants_user_skill(text)
        native_attempts = 4 if skill_boost else 2
        prompt_attempts = 4 if skill_boost else 2

        # Try native tool calling first if we haven't ruled it out.
        all_tools = TOOLS_SPEC + self._user_tool_specs
        native_tools = (
            _skill_authoring_tool_specs(all_tools) if skill_boost else all_tools
        )
        if native_supported is not False:
            effective_text = text
            for attempt in range(native_attempts):
                try:
                    resp = self._lm_native_chat(
                        model=model,
                        messages=self._build_messages(
                            effective_text, user, "native",
                            skill_authoring=skill_boost,
                        ),
                        tools=native_tools,
                        temperature=0.3,
                    )
                except Exception as e:
                    msg = str(e)
                    err_resp = getattr(e, "response", None)
                    if err_resp is not None:
                        try:
                            msg = (
                                f"{msg} {err_resp.status_code} "
                                f"{(err_resp.text or '')[:1200]}"
                            )
                        except Exception:
                            pass
                    if ("does not support tools" in msg
                            or "tools" in msg.lower() and "400" in msg
                            or (self._llm_provider == "lm_studio"
                                and "400" in msg
                                and "tool" in msg.lower())):
                        log.info(
                            "model %s lacks native tools; using prompt-based fallback",
                            model,
                        )
                        self._tool_support[model] = False
                        break
                    log.warning("llm chat failed: %s", e)
                    return SkillResult(
                        self._lm_unreachable_msg(),
                        intent="chat", success=False,
                    )
                self._tool_support[model] = True
                tool_calls = _extract_tool_calls(resp)
                narration = _extract_content(resp)
                finish_reason = _openai_finish_reason(resp)
                # Some models (qwen3-coder) emit XML-style tool calls inside
                # the content instead of via tool_calls. Harvest those too.
                if "<function=" in narration:
                    tool_calls += _parse_xml_tool_calls(narration)
                    narration = _XML_FN_RE.sub("", narration)
                    narration = re.sub(r"</?tool_call>", "", narration).strip()
                if skill_boost and not tool_calls and finish_reason == "length":
                    log.warning(
                        "skill authoring: native pass hit finish_reason=length; retry %s/%s",
                        attempt,
                        native_attempts - 1,
                    )
                    if attempt < native_attempts - 1:
                        effective_text = (
                            text
                            + "\n\n[Previous output was truncated before a valid tool call. "
                            "Retry with ONE compact tool call only. Do not emit reasoning. "
                            "Call propose_patch now.]"
                        )
                        continue
                if (skill_boost and tool_calls
                        and not _skill_authoring_tool_calls_ok(tool_calls)):
                    log.warning(
                        "skill authoring: native pass returned %s; retry %s/%s",
                        [t[0] for t in tool_calls],
                        attempt,
                        native_attempts - 1,
                    )
                    if attempt < native_attempts - 1:
                        effective_text = text + _skill_authoring_native_nudge(
                            tool_calls, attempt,
                        )
                        continue
                    return self._finalise(
                        text,
                        [],
                        narration=_skill_authoring_exhausted_reply(),
                        user=user,
                        on_status=on_status,
                    )
                return self._finalise(
                    text, tool_calls, narration, user, on_status=on_status
                )

        # Prompt-based fallback: the model replies with raw JSON or plain text.
        try:
            effective = text
            resp = None
            for attempt in range(prompt_attempts):
                resp = self._lm_native_chat(
                    model=model,
                    messages=self._build_messages(
                        effective, user, "prompt", skill_authoring=skill_boost,
                    ),
                    tools=None,
                    temperature=0.2,
                )
                content = _extract_content(resp)
                parsed = self._parse_prompted_tool(content)
                if parsed is None:
                    if skill_boost and attempt < prompt_attempts - 1:
                        log.warning(
                            "skill authoring: prompt pass returned no tool JSON; "
                            "retry %s/%s",
                            attempt,
                            prompt_attempts - 1,
                        )
                        effective = (
                            text
                            + _skill_authoring_prompt_nudge(
                                "(no tool in reply)", attempt,
                            )
                        )
                        continue
                    return self._finalise(
                        text, [], narration=content, user=user,
                        on_status=on_status,
                    )
                pname, _ = parsed
                if skill_boost and pname not in (
                        "propose_patch", "create_user_skill"):
                    log.warning(
                        "skill authoring: prompt pass returned tool %r; retry %s/%s",
                        pname,
                        attempt,
                        prompt_attempts - 1,
                    )
                    if attempt < prompt_attempts - 1:
                        effective = text + _skill_authoring_prompt_nudge(
                            pname, attempt,
                        )
                        continue
                    return self._finalise(
                        text,
                        [],
                        narration=_skill_authoring_exhausted_reply(),
                        user=user,
                        on_status=on_status,
                    )
                return self._finalise(
                    text, [parsed], narration="", user=user,
                    on_status=on_status,
                )
        except Exception as e:
            log.warning("llm chat fallback failed: %s", e)
            return SkillResult(
                self._lm_unreachable_msg(),
                intent="chat", success=False,
            )

    def _finalise(
        self,
        text: str,
        tool_calls: list[tuple[str, dict]],
        narration: str,
        user: str,
        on_status: Callable[[str], None] | None = None,
    ) -> SkillResult:
        if tool_calls:
            if on_status is not None:
                msg = _tool_narration_message(tool_calls)
                if msg:
                    try:
                        on_status(msg)
                    except Exception:  # pragma: no cover
                        log.debug("on_status callback failed", exc_info=True)
            results: list[SkillResult] = []
            for name, args in tool_calls:
                log.info("tool_call: %s(%s)", name, args)
                r = self._run_tool(name, args, user)
                if _wants_user_skill(text) and r.intent == "unknown_tool":
                    r = SkillResult(
                        "You asked for a new skill, but I tried to run a tool "
                        "that does not exist yet. I should use propose_patch on "
                        "user_skills/skills.py instead—please repeat your request.",
                        intent="skill_authoring",
                        success=False,
                    )
                results.append(r)
            all_ok = all(r.success for r in results)
            tool_reply = " ".join(r.reply for r in results if r.reply).strip()
            # Only let narration override the tool reply when it's substantive
            # (>= 12 chars AND has alphabetic content). Coder models sometimes
            # leave XML fragments as narration; we don't want those to hide a
            # real error/success message from the tool handler.
            narr_stripped = narration.strip()
            narr_has_words = any(c.isalpha() for c in narr_stripped)
            if narr_has_words and len(narr_stripped) >= 12:
                reply = narr_stripped
            else:
                reply = tool_reply or "Done."
            intent = results[0].intent if len(results) == 1 else "tool_chain"
            self._history.append(("user", text))
            self._history.append(("assistant", reply))
            return SkillResult(reply, intent=intent, success=all_ok)

        reply = narration or "I'm not sure how to help with that."
        self._history.append(("user", text))
        self._history.append(("assistant", reply))
        return SkillResult(reply, intent="chat")

    def reset_history(self) -> None:
        self._history.clear()

    def set_model(self, model: str) -> None:
        model = (model or "").strip()
        if not model:
            return
        self.llm_cfg.model = model
        log.info("LLM model changed to %s", model)

    def register_user_skill(self, name: str, description: str,
                            parameters: dict,
                            fn: Callable[[dict], SkillResult]) -> None:
        """Register a Stage-2 user skill as a new LLM tool. Overwrites any
        previous registration of the same name."""
        self._user_skills[name] = fn
        self._user_tool_specs = [
            s for s in self._user_tool_specs
            if s.get("function", {}).get("name") != name
        ]
        self._user_tool_specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })
        log.info("registered user skill: %s", name)

    def unregister_user_skill(self, name: str) -> None:
        self._user_skills.pop(name, None)
        self._user_tool_specs = [
            s for s in self._user_tool_specs
            if s.get("function", {}).get("name") != name
        ]

    def list_user_skills(self) -> list[str]:
        return sorted(self._user_skills.keys())

    def list_models(self) -> list[str]:
        if self._llm_provider == "lm_studio":
            try:
                from .openai_compat import list_models as openai_list_models

                return openai_list_models(
                    self.llm_cfg.openai_base_url,
                    self.llm_cfg.openai_api_key,
                )
            except Exception as e:
                log.warning("lm studio list models failed: %s", e)
                return []
        if self._ollama is None:
            return []
        try:
            resp = self._ollama.list()
            models = getattr(resp, "models", None) or resp.get("models", []) if isinstance(resp, dict) else resp.models
            out: list[str] = []
            for m in models:
                name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None) \
                       or getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None)
                if name:
                    out.append(name)
            return sorted(set(out))
        except Exception as e:
            log.warning("ollama list failed: %s", e)
            return []
