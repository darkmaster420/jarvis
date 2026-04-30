"""Tool dispatch logic extracted from orchestrator."""
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
        }.get(name, name)
        if not self._authorised(intent, user):
            return self._restricted_denied
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
        if name == "start_mongodb_container":
            return system.start_mongodb_container()
        if name == "web_search":
            query = (args.get("query") or args.get("q") or "").strip()
            return web.search(query)
        if name == "open_url":
            url = (args.get("url") or "").strip()
            return web.open_url(url)
        if name == "get_system_stats":
            return info_skill.system_stats()
        if name == "run_terminal_command":
            timeout_raw = args.get("timeout_s")
            try:
                timeout_s = int(timeout_raw) if timeout_raw is not None else 25
            except (TypeError, ValueError):
                timeout_s = 25
            return terminal_skill.run(
                command=(args.get("command") or "").strip(),
                cwd=(args.get("cwd") or "").strip() or None,
                timeout_s=timeout_s,
                allow_dangerous=bool(args.get("allow_dangerous", False)),
            )
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
            return SkillResult("Got it, I'll remember that.", intent="remember")
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
            return SkillResult(f"Okay, '{phrase}' will open {target}.", intent="add_alias")
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
            rname = (args.get("name") or "").strip()
            steps = self.memory.get_routine(rname) if rname else None
            if not steps:
                return SkillResult(f"I don't have a routine called '{rname}'.",
                                   intent="run_routine", success=False)
            replies: list[str] = []
            ok_all = True
            for step in steps:
                r = self._handle_text(step, user)
                if not r.success:
                    ok_all = False
                if r.reply:
                    replies.append(r.reply)
            summary = f"Ran '{rname}': " + "; ".join(replies) if replies else f"Ran '{rname}'."
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
            return self.user_skills_mgr.remove((args.get("name") or "").strip())
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
                return SkillResult(
                    f"Patch rejected before HUD review: {e}",
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

