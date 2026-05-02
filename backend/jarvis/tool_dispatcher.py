# backend/jarvis/tool_dispatcher.py
"""
Tool dispatch logic extracted from orchestrator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .skills import system, web
from .skills import info as info_skill
from .skills import terminal as terminal_skill
from .skills.base import SkillResult

class ToolDispatcher:
    def __init__(
        self,
        *,
        memory: Any,
        user_skills_mgr: Any,
        patches: Any,
        user_skills_ref: dict[str, Callable[[dict], SkillResult]],
        authorised: Callable[[str, str], bool],
        restricted_denied: SkillResult,
        handle_text: Callable[[str, str], SkillResult],
    ) -> None:
        self.memory = memory
        self.user_skills_mgr = user_skills_mgr
        self.patches = patches
        self._user_skills_ref = user_skills_ref
        self._authorised = authorised
        self._restricted_denied = restricted_denied
        self._handle_text = handle_text

    def run_tool(self, name: str, args: dict, user: str) -> SkillResult:
        # Some models call handler symbols (e.g. handle_check_storage) instead
        # of the exposed tool name (check_storage). Normalize when possible.
        if (
            isinstance(name, str)
            and name.startswith("handle_")
            and name[7:] in self._user_skills_ref
        ):
            name = name[7:]
        intent = {
            "open_app":   "open_app",
            "close_app":  "close_app",
            "start_mongodb_container": "start_mongodb_container",
            "close_browser_tab": "close_browser_tab",
            "reopen_closed_browser_tab": "reopen_closed_browser_tab",
            "web_search": "web_search",
            "open_url":   "open_url",
            "get_system_stats": "sys_stats",
            "run_terminal_command": "terminal_exec",
            "open_games_folder": "open_games_folder"  # <-- New mapping
        }.get(name, name)
        if not self._authorised(intent, user):
            return self._restricted_denied
        if name == "close_browser_tab":
            return web.close_browser_tab()
        if name == "reopen_closed_browser_tab":
            return web.reopen_closed_browser_tab()
        if name == "open_app":
            target = (args.get("name") or args.get("app") or ".").strip()
            return system.open_app(target)
        if name == "close_app":
            target = (args.get("name") or args.get("app") or ".").strip()
            return system.close_app(target)
        if name == "start_mongodb_container":
            return system.start_mongodb_container()
        if name == "web_search":
            query = (args.get("query") or args.get("q") or ".").strip()
            return web.search(query)
        if name == "open_url":
            url = (args.get("url") or ".").strip()
            return web.open_url(url)
        if name == "get_system_stats":
            return info_skill.system_stats()
        if name == "run_terminal_command":
            timeout_raw = args.get("timeout_s")
            try:
                timeout_s = int(timeout_raw) if timeout_raw else 30
            except ValueError:
                timeout_s = 30
            cmd = (args.get("command") or ".").strip()
            return terminal_skill.run(cmd, timeout_s)
        if name == "open_games_folder":
            # Windows-specific: open the Games folder using shell
            import os
            import subprocess
            try:
                games_path = os.path.join(os.environ["USERPROFILE"], "Games")
                if not os.path.exists(games_path):
                    return SkillResult("Games folder does not exist.", intent="open_games_folder", success=False)
                # Use shell to open the folder
                subprocess.run(["explorer.exe", games_path])
                return SkillResult("Opened Games folder.", intent="open_games_folder", success=True)
            except Exception as e:
                return SkillResult(f"Failed to open Games folder: {e}", intent="open_games_folder", success=False)
        if name in self._user_skills_ref:
            try:
                return self._user_skills_ref[name](args)
            except Exception as e:
                return SkillResult(f"User skill {name!r} crashed: {e}",
                                   intent=name, success=False)
        return SkillResult(
            f"I don't know how to do {name!r}.",
            intent="unknown_tool", success=False,
        )
