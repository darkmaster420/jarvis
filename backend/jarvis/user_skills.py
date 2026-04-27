"""User-defined tools (skills).

**Preferred:** a single bundle file ``user_skills/skills.py`` (under the app
data directory, e.g. ``%LocalAppData%/Jarvis/user_skills``). It defines
``SKILLS = {name: {description, parameters}}``
and one ``handle_<name>(args: dict) -> dict`` per tool. The LLM should edit it
via ``propose_patch`` with target ``user_skills/skills.py`` (full file); after
HUD approval the server **hot-reloads** tools without restart.

**Legacy:** separate ``*.py`` modules in the same folder (each with
``PARAMETERS`` and ``handle``) still load if their stem is not already taken by
the bundle.

Safety model:
  * AST scan before load/write; small stdlib import allowlist; banned names
    (``exec``, ``open``, …).
  * New modules are smoke-tested in a subprocess before replace.
  * Git commits are best-effort when available.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

from .skills.base import SkillResult

log = logging.getLogger(__name__)


# Modules generated skills are allowed to ``import``. Keep this minimal -
# expand it deliberately as real use-cases come up.
ALLOWED_IMPORTS: set[str] = {
    "json", "re", "math", "random", "datetime", "time", "statistics",
    "string", "textwrap", "collections", "itertools", "functools",
    "operator", "dataclasses", "typing", "fractions", "decimal",
    "calendar", "enum",
    # Safe URL / browser helpers (no raw sockets); used for “open in browser”
    # and small read-only fetches. Avoid secrets in URLs.
    "webbrowser", "urllib",
}

# Names the sandbox never allows to appear in source (even as strings
# inside getattr would require getattr itself, which we ban).
BANNED_NAMES: set[str] = {
    "exec", "eval", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "__subclasses__", "__bases__", "__mro__", "__class__", "__globals__",
    "__builtins__",
}

# Matches a legal Python identifier AND a nice filename.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

BUNDLE_MODULE = "jarvis_user_skills.bundle"

_DEFAULT_SKILLS_PY = '''\
"""User-defined Jarvis tools (single bundle).

Edit with propose_patch target ``user_skills/skills.py`` (send the FULL file).
After you approve the patch in the HUD, tools reload without restarting Jarvis.

Each ``SKILLS`` entry: ``name -> {"description": str, "parameters": JSON schema}``.
Implement ``handle_<name>(args: dict) -> dict`` with keys ``reply`` and ``success``.

Same sandbox as other user skills: allowlisted stdlib only; no subprocess,
``open``, ``eval``, etc.
"""

SKILLS: dict[str, dict] = {
    # Example (uncomment and edit):
    # "hello": {
    #     "description": "Say hello.",
    #     "parameters": {
    #         "type": "object",
    #         "properties": {},
    #         "required": [],
    #     },
    # },
}


# def handle_hello(args: dict) -> dict:
#     return {"reply": "Hello!", "success": True}
'''


class SkillValidationError(ValueError):
    """Raised when an LLM-proposed skill fails the sandbox checks."""


def _scan_ast(source: str) -> None:
    """Reject the code if it does anything sketchy. Raises on violations."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SkillValidationError(f"syntax error: {e}")

    for node in ast.walk(tree):
        # Import statements must target ALLOWED_IMPORTS only.
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise SkillValidationError(
                        f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise SkillValidationError(
                    f"import from '{node.module}' is not allowed")
        # Banned names anywhere in the AST.
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise SkillValidationError(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            raise SkillValidationError(
                f"attribute '{node.attr}' is not allowed")

    # Must define handle().
    has_handle = any(
        isinstance(n, ast.FunctionDef) and n.name == "handle"
        for n in tree.body
    )
    if not has_handle:
        raise SkillValidationError("module must define `def handle(args)`")
    # Must define PARAMETERS as an assignment.
    has_params = any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PARAMETERS" for t in n.targets)
        for n in tree.body
    )
    if not has_params:
        raise SkillValidationError(
            "module must define `PARAMETERS = {...}` (JSON schema)")


def _scan_ast_bundle(source: str) -> None:
    """Validate ``skills.py`` bundle: same security as single-file skills, but
    require ``SKILLS = {...}`` instead of a lone ``handle``/``PARAMETERS``."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SkillValidationError(f"syntax error: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise SkillValidationError(
                        f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise SkillValidationError(
                    f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise SkillValidationError(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            raise SkillValidationError(
                f"attribute '{node.attr}' is not allowed")

    has_skills = any(
        isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "SKILLS"
            for t in n.targets
        )
        for n in tree.body
    )
    if not has_skills:
        raise SkillValidationError(
            "skills.py must define `SKILLS = { ... }`")


def _smoke_test(module_path: Path, timeout: float = 5.0) -> None:
    """Run the module in a subprocess to confirm it imports cleanly and has
    the right shape. We don't call handle() - we just verify structure."""
    probe = textwrap.dedent(f"""
        import importlib.util, sys, json
        spec = importlib.util.spec_from_file_location("probe", r"{module_path}")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(getattr(mod, "handle", None)), "no handle()"
        assert isinstance(getattr(mod, "PARAMETERS", None), dict), "bad PARAMETERS"
        assert isinstance(mod.__doc__, str) and mod.__doc__.strip(), "missing docstring"
        print("ok")
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SkillValidationError("smoke test timed out")
    if proc.returncode != 0 or "ok" not in proc.stdout:
        raise SkillValidationError(
            "smoke test failed: " + (proc.stderr or proc.stdout).strip())


def _git_commit(repo: Path, files: list[Path], message: str) -> None:
    """Best-effort git commit. Silently skips if the repo isn't a git
    checkout or git isn't available."""
    try:
        subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                       capture_output=True, check=True, timeout=5)
    except Exception:
        return
    try:
        subprocess.run(["git", "-C", str(repo), "add"] + [str(f) for f in files],
                       capture_output=True, check=True, timeout=10)
        subprocess.run(["git", "-C", str(repo), "commit",
                        "-m", message, "--no-verify"],
                       capture_output=True, check=True, timeout=10)
    except Exception as e:
        log.info("git commit skipped: %s", e)


def _handle_result(raw: Any, skill_name: str) -> SkillResult:
    """Normalise whatever the user skill returned into a SkillResult."""
    if isinstance(raw, str):
        return SkillResult(raw or "Done.", intent=skill_name)
    if isinstance(raw, dict):
        return SkillResult(
            str(raw.get("reply", "Done.")),
            intent=skill_name,
            success=bool(raw.get("success", True)),
        )
    return SkillResult("Done.", intent=skill_name)


class UserSkillManager:
    """Loads and manages user-authored skills. Delegates LLM registration
    back to the orchestrator (passed in via ``bind``)."""

    def __init__(self, skills_dir: Path, repo_root: Path):
        self.dir  = Path(skills_dir)
        self.root = Path(repo_root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._register: Callable[..., None] | None = None
        self._unregister: Callable[[str], None] | None = None
        self._managed: set[str] = set()
        self._from_bundle: set[str] = set()
        self._bundle_meta: dict[str, str] = {}
        self._ensure_bundle()

    def bind(self, register, unregister) -> None:
        self._register   = register
        self._unregister = unregister

    def _ensure_bundle(self) -> None:
        bundle = self.dir / "skills.py"
        if not bundle.exists():
            bundle.write_text(_DEFAULT_SKILLS_PY, encoding="utf-8")

    def read_bundle_text(self) -> str:
        """Ensure ``skills.py`` exists and return its full source for LLM prompts."""
        self._ensure_bundle()
        path = self.dir / "skills.py"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("read_bundle_text failed: %s", e)
            return _DEFAULT_SKILLS_PY

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    def reload_all(self) -> None:
        """Unregister every user tool we registered, then load ``skills.py``
        plus legacy ``*.py`` modules from disk (for HUD hot-reload)."""
        if self._register is None or self._unregister is None:
            log.warning("UserSkillManager.reload_all called before bind()")
            return
        for n in list(self._managed):
            self._unregister(n)
        self._managed.clear()
        self._from_bundle.clear()
        self._bundle_meta.clear()
        self._ensure_bundle()
        try:
            self._load_bundle()
        except Exception as e:
            log.warning("failed to load user_skills/skills.py bundle: %s", e)
        for py in sorted(self.dir.glob("*.py")):
            if py.name.startswith("_") or py.name == "skills.py":
                continue
            if py.stem in self._from_bundle:
                log.warning(
                    "skipping legacy user skill %s (name already in skills.py bundle)",
                    py.name)
                continue
            try:
                self._load_file(py)
            except Exception as e:
                log.warning("failed to load user skill %s: %s", py.name, e)

    def load_all(self) -> None:
        self.reload_all()

    def _load_bundle(self) -> None:
        path = self.dir / "skills.py"
        if not path.exists():
            return
        src = path.read_text(encoding="utf-8")
        _scan_ast_bundle(src)
        sys.modules.pop(BUNDLE_MODULE, None)
        spec = importlib.util.spec_from_file_location(BUNDLE_MODULE, path)
        if spec is None or spec.loader is None:
            raise SkillValidationError("could not create bundle module spec")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        skills_dict = getattr(mod, "SKILLS", None)
        if not isinstance(skills_dict, dict):
            raise SkillValidationError("SKILLS must be a dict[str, dict]")

        to_register: list[tuple[str, str, dict, Callable]] = []
        for name, meta in skills_dict.items():
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise SkillValidationError(
                    f"SKILLS key {name!r} must be a valid tool name "
                    "(lowercase, 3-32 chars, a-z0-9_)")
            if not isinstance(meta, dict):
                raise SkillValidationError(
                    f"SKILLS[{name!r}] must be a dict with "
                    "'description' and 'parameters'")
            description = (meta.get("description") or "").strip()
            if not description:
                raise SkillValidationError(
                    f"SKILLS[{name!r}] needs a non-empty description")
            parameters = meta.get("parameters")
            if not isinstance(parameters, dict):
                raise SkillValidationError(
                    f"SKILLS[{name!r}]['parameters'] must be a JSON schema dict")
            handler_name = f"handle_{name}"
            handle = getattr(mod, handler_name, None)
            if not callable(handle):
                raise SkillValidationError(
                    f"bundle must define def {handler_name}(args: dict) -> dict")
            to_register.append((name, description, parameters, handle))

        assert self._register is not None
        for name, description, parameters, handle in to_register:
            def _call(
                args: dict,
                _h: Callable = handle,
                _n: str = name,
            ) -> SkillResult:
                return _handle_result(_h(args or {}), _n)

            self._register(name, description, parameters, _call)
            self._managed.add(name)
            self._from_bundle.add(name)
            self._bundle_meta[name] = description

    def _load_file(self, path: Path) -> None:
        name = path.stem
        _scan_ast(path.read_text(encoding="utf-8"))  # defence-in-depth on restart
        spec = importlib.util.spec_from_file_location(
            f"jarvis_user_skills.{name}", path)
        if spec is None or spec.loader is None:
            raise SkillValidationError("could not create module spec")
        mod = importlib.util.module_from_spec(spec)
        # Clear any cached copy so edits take effect on reload.
        sys.modules.pop(spec.name, None)
        spec.loader.exec_module(mod)
        description = (mod.__doc__ or f"User-defined skill {name}").strip()
        parameters  = getattr(mod, "PARAMETERS", None)
        handle      = getattr(mod, "handle", None)
        if not isinstance(parameters, dict) or not callable(handle):
            raise SkillValidationError("missing PARAMETERS or handle()")

        def _call(args: dict, _h=handle) -> SkillResult:
            return _handle_result(_h(args or {}), name)

        assert self._register is not None
        self._register(name, description, parameters, _call)
        self._managed.add(name)

    # ------------------------------------------------------------------
    # authoring
    # ------------------------------------------------------------------
    def create(self, name: str, description: str, code: str) -> SkillResult:
        if not _NAME_RE.match(name):
            return SkillResult(
                "Skill name must be lowercase, start with a letter, and "
                "be 3-32 chars (a-z, 0-9, _).",
                intent="create_user_skill", success=False)
        if not description.strip():
            return SkillResult("Description is required.",
                               intent="create_user_skill", success=False)
        if not code.strip():
            return SkillResult("Code is required.",
                               intent="create_user_skill", success=False)

        # Guarantee we keep the description at the top for the loader.
        if not code.lstrip().startswith(('"""', "'''")):
            safe_desc = description.replace('"""', '\\"\\"\\"')
            code = f'"""{safe_desc}"""\n\n' + code.lstrip()

        try:
            _scan_ast(code)
        except SkillValidationError as e:
            return SkillResult(f"Skill rejected: {e}",
                               intent="create_user_skill", success=False)

        if name in self._from_bundle:
            return SkillResult(
                f"A tool named '{name}' is already defined in user_skills/skills.py. "
                "Edit the bundle with propose_patch (target user_skills/skills.py) "
                "or remove it from SKILLS there first.",
                intent="create_user_skill", success=False)

        target = self.dir / f"{name}.py"
        if target.exists():
            return SkillResult(
                f"A skill called '{name}' already exists. Pick a new name or "
                "ask me to remove the old one first.",
                intent="create_user_skill", success=False)

        tmp = target.with_suffix(".py.tmp")
        tmp.write_text(code, encoding="utf-8")
        try:
            _smoke_test(tmp)
        except SkillValidationError as e:
            tmp.unlink(missing_ok=True)
            return SkillResult(f"Skill rejected: {e}",
                               intent="create_user_skill", success=False)
        tmp.replace(target)

        try:
            self._load_file(target)
        except Exception as e:
            target.unlink(missing_ok=True)
            return SkillResult(f"Failed to register skill: {e}",
                               intent="create_user_skill", success=False)

        _git_commit(self.root, [target],
                    f"[jarvis] add user skill: {name}\n\n{description}")
        return SkillResult(
            f"Added skill '{name}'. I can now use it on your behalf.",
            intent="create_user_skill")

    def remove(self, name: str) -> SkillResult:
        if not _NAME_RE.match(name):
            return SkillResult("Bad skill name.",
                               intent="remove_user_skill", success=False)
        if name in self._from_bundle:
            return SkillResult(
                f"'{name}' is defined inside user_skills/skills.py (bundle). "
                f"Remove its SKILLS entry and the handle_{name} function there, "
                "then propose_patch with the full file; after approval I hot-reload "
                "tools.",
                intent="remove_user_skill", success=False)
        target = self.dir / f"{name}.py"
        if not target.exists():
            return SkillResult(f"No skill called '{name}'.",
                               intent="remove_user_skill", success=False)
        target.unlink()
        if self._unregister is not None:
            self._unregister(name)
        self._managed.discard(name)
        _git_commit(self.root, [target], f"[jarvis] remove user skill: {name}")
        return SkillResult(f"Removed skill '{name}'.",
                           intent="remove_user_skill")

    def list(self) -> list[dict]:
        out: list[dict] = []
        for tool_name in sorted(self._bundle_meta.keys()):
            out.append({
                "name": tool_name,
                "description": self._bundle_meta[tool_name],
                "source": "bundle",
            })
        for py in sorted(self.dir.glob("*.py")):
            if py.name.startswith("_") or py.name == "skills.py":
                continue
            if py.stem in self._from_bundle:
                continue
            try:
                src = py.read_text(encoding="utf-8")
                tree = ast.parse(src)
                doc = ast.get_docstring(tree) or ""
            except Exception:
                doc = ""
            out.append({
                "name": py.stem,
                "description": doc.strip(),
                "source": "legacy",
            })
        return out
