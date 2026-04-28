"""Prompt construction helpers for orchestrator chat/tool flows."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class PromptBuilder:
    def __init__(
        self,
        llm_cfg: Any,
        memory: Any,
        user_skills_mgr: Any,
        user_tool_specs_ref: list[dict],
        user_skill_bundle_guide: str,
    ) -> None:
        self.llm_cfg = llm_cfg
        self.memory = memory
        self.user_skills_mgr = user_skills_mgr
        self._user_tool_specs_ref = user_tool_specs_ref
        self._user_skill_bundle_guide = user_skill_bundle_guide

    def skills_bundle_location_hint(self) -> str:
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

    def skill_authoring_context(self) -> str:
        """Extra system text + live ``skills.py`` so the model can propose_patch."""
        body = ""
        if self.user_skills_mgr is not None:
            try:
                body = self.user_skills_mgr.read_bundle_text()
            except Exception:
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

    @staticmethod
    def core_authoring_context() -> str:
        return (
            "\n\n=== THIS TURN: CORE EXTENSION ===\n"
            "The user requested a built-in capability that likely needs backend "
            "changes (not only user_skills). Use propose_patch targeting "
            "backend/jarvis/*.py with FULL file content. Choose the smallest "
            "relevant file(s) and include safe validation/error handling. "
            "Do not call invented tools; emit propose_patch.\n"
        )

    def prompt_tool_instructions(self, tools_spec: list[dict]) -> str:
        specs = tools_spec + self._user_tool_specs_ref
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
        hint = self.skills_bundle_location_hint()
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
            "- For explicit shell/terminal requests (ping, winget, git, command line file ops), use run_terminal_command. Set allow_dangerous=true for destructive commands.",
            "",
            self._user_skill_bundle_guide,
            "",
            "If a tool is needed, respond with EXACTLY one JSON object on a single line, no prose:",
            "  {\"tool\": \"<name>\", \"args\": {<args>}}",
            "If no tool is needed, reply in plain natural English (1-2 sentences), with NO JSON.",
        ])
        return "\n".join(lines)

    def build_messages(
        self,
        text: str,
        user: str,
        tool_style: str,
        *,
        skill_authoring: bool,
        core_authoring: bool,
        tools_spec: list[dict],
        history: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        sys_prompt = self.llm_cfg.system_prompt
        if tool_style == "prompt":
            sys_prompt = sys_prompt + "\n\n" + self.prompt_tool_instructions(tools_spec)
        elif core_authoring:
            sys_prompt = sys_prompt + (
                "\n"
                "CORE EXTENSION MODE (native tool call):\n"
                "- Use ONLY propose_patch.\n"
                "- Target backend/jarvis/<file>.py for built-in capability updates.\n"
                "- Return one valid tool call, no prose/reasoning.\n"
            )
        elif skill_authoring:
            sys_prompt = sys_prompt + (
                "\n"
                "SKILL AUTHORING MODE (native tool call):\n"
                "- Use ONLY propose_patch or create_user_skill.\n"
                "- Prefer propose_patch on user_skills/skills.py (or absolute path).\n"
                "- Do NOT invent tool names.\n"
                "- Return a valid tool call, not prose.\n"
            ) + self.skills_bundle_location_hint()
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
                "- If the user asks for terminal/shell commands (ping, winget, "
                "git, command-line file operations), call run_terminal_command. "
                "For destructive commands, set allow_dangerous=true.\n"
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
            ) + self._user_skill_bundle_guide + "\n" + self.skills_bundle_location_hint()
        if skill_authoring:
            sys_prompt = sys_prompt + self.skill_authoring_context()
        if core_authoring:
            sys_prompt = sys_prompt + self.core_authoring_context()
        if user and user != "guest":
            sys_prompt = sys_prompt + f"\nThe speaker's name is {user.title()}."
        if self.memory is not None:
            mem_block = self.memory.context_block(text)
            if mem_block:
                sys_prompt = sys_prompt + "\n\n" + mem_block
        msgs: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        for role, content in history:
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": text})
        return msgs

