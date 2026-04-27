"""Stage-3 "self-improvement": the LLM can propose patches to its own
core code, but those patches *only* apply after the user approves them
in the HUD.

Flow:
  1. LLM calls ``propose_patch(target, description, new_content)``.
  2. We validate (path is inside an allow-listed directory, result is
     valid Python, file exists to amend) and write a JSON record into
     ``proposed_patches/<id>.json``.
  3. The HUD lists pending patches and shows a unified diff.
  4. User clicks approve -> we re-validate, swap the file, run
     ``python -m py_compile`` on it, and git-commit. On failure the
     file is restored from its pre-patch snapshot.
  5. User clicks reject -> the patch file is deleted.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Relative paths (vs repo root) the LLM is allowed to patch. Anything
# outside is rejected. Extend cautiously.
ALLOWED_PATCH_PREFIXES: tuple[str, ...] = (
    "backend/jarvis/",
    "user_skills/",
)

# Files the LLM must never touch even if inside an allowed prefix.
BANNED_PATCH_PATHS: frozenset[str] = frozenset({
    "backend/jarvis/__init__.py",
})


class PatchError(ValueError):
    pass


def _resolve_patch_target(
    repo: Path,
    raw: str,
    *,
    user_skills_dir: Path | None = None,
) -> tuple[str, Path]:
    """Map a propose_patch target string to (canonical relative path, abs path).

    Accepts the usual virtual paths (``user_skills/skills.py``, ``backend/...``)
    or an **absolute** path that resolves inside the user_skills data directory
    or the repo — so models can pass the real on-disk bundle path.
    """
    raw = (raw or "").strip()
    if not raw:
        raise PatchError("empty target path")

    repo_r = repo.resolve()
    us_r = (
        Path(user_skills_dir).resolve()
        if user_skills_dir is not None
        else None
    )

    p_in = Path(raw)
    if p_in.is_absolute():
        abs_in = p_in.expanduser().resolve()
        rel_canon: str | None = None
        if us_r is not None:
            try:
                sub = abs_in.relative_to(us_r)
                rel_canon = "user_skills/" + sub.as_posix()
            except ValueError:
                pass
        if rel_canon is None:
            try:
                sub = abs_in.relative_to(repo_r)
                rel_canon = sub.as_posix()
            except ValueError:
                raise PatchError(
                    "target must be inside the app/repo tree or the Jarvis "
                    "user_skills data folder (typically …/user_skills/skills.py "
                    "under %LocalAppData%/Jarvis or your configured data dir)"
                )
    else:
        rel_canon = raw.replace("\\", "/")

    if ".." in rel_canon.split("/"):
        raise PatchError("target path may not contain '..'")
    if not any(rel_canon.startswith(p) for p in ALLOWED_PATCH_PREFIXES):
        raise PatchError(
            f"target must be inside one of: {', '.join(ALLOWED_PATCH_PREFIXES)}")
    if rel_canon in BANNED_PATCH_PATHS:
        raise PatchError(f"{rel_canon} is on the patch denylist")
    if rel_canon.startswith("user_skills/"):
        if us_r is None:
            raise PatchError("user_skills/ targets require a user data directory")
        sub = rel_canon[len("user_skills/") :]
        if not sub or sub.startswith("/"):
            raise PatchError("invalid user_skills path")
        abs_path = (us_r / sub).resolve()
        try:
            abs_path.relative_to(us_r)
        except ValueError:
            raise PatchError("user_skills target escapes user_skills directory")
        return rel_canon, abs_path
    abs_path = (repo_r / rel_canon).resolve()
    try:
        abs_path.relative_to(repo_r)
    except ValueError:
        raise PatchError("target escapes the repo root")
    return rel_canon, abs_path


def _norm_target(
    repo: Path,
    rel: str,
    *,
    user_skills_dir: Path | None = None,
) -> Path:
    _, abs_path = _resolve_patch_target(
        repo, rel, user_skills_dir=user_skills_dir)
    return abs_path


def _unified_diff(before: str, after: str, label: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{label}", tofile=f"b/{label}", n=3,
    ))


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _git_commit(repo: Path, paths: list[Path], message: str) -> None:
    try:
        subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                       capture_output=True, check=True, timeout=5)
    except Exception:
        return
    try:
        subprocess.run(["git", "-C", str(repo), "add"]
                       + [str(p) for p in paths],
                       capture_output=True, check=True, timeout=10)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", message,
                        "--no-verify"], capture_output=True, check=True,
                       timeout=10)
    except Exception as e:
        log.info("git commit skipped: %s", e)


class PatchManager:
    """JSON-file-backed store for pending LLM-proposed patches.

    * ``repo_root`` — app install dir; patch *targets* resolve under it (e.g. backend/jarvis/…).
    * ``patch_dir`` — where pending JSON is stored; defaults to ``<repo_root>/proposed_patches``,
      or e.g. ``%LOCALAPPDATA%/Jarvis/proposed_patches`` when using AppData.
    """

    def __init__(
        self,
        repo_root: Path,
        patch_dir: Path | None = None,
        user_skills_dir: Path | None = None,
    ):
        self.root = Path(repo_root)
        self.dir = Path(patch_dir) if patch_dir is not None else self.root / "proposed_patches"
        self.user_skills_dir = (
            Path(user_skills_dir) if user_skills_dir is not None else None
        )
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # listing
    # ------------------------------------------------------------------
    def _patch_path(self, patch_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{6,32}", patch_id):
            raise PatchError("bad patch id")
        return self.dir / f"{patch_id}.json"

    def list_patches(self) -> list[dict]:
        out: list[dict] = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            # Don't leak enormous file bodies on list(); summarise.
            out.append({
                "id":          data.get("id"),
                "target":      data.get("target"),
                "description": data.get("description"),
                "diff":        data.get("diff"),
                "created":     data.get("created"),
            })
        out.sort(key=lambda d: d.get("created", 0.0))
        return out

    def get(self, patch_id: str) -> dict:
        p = self._patch_path(patch_id)
        if not p.exists():
            raise PatchError(f"no such patch: {patch_id}")
        return json.loads(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # proposing
    # ------------------------------------------------------------------
    def propose(self, target: str, description: str, new_content: str) -> dict:
        description = (description or "").strip()
        if not description:
            raise PatchError("description is required")
        if not new_content.endswith("\n"):
            new_content = new_content + "\n"
        rel_canon, abs_path = _resolve_patch_target(
            self.root, target, user_skills_dir=self.user_skills_dir)
        if not abs_path.exists():
            raise PatchError(
                f"{target} doesn't exist; seed user_skills/skills.py first or "
                "use create_user_skill for a new module file")
        before = abs_path.read_text(encoding="utf-8")
        if before == new_content:
            raise PatchError("no changes vs current file")
        if abs_path.suffix == ".py":
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                raise PatchError(f"proposed content has a syntax error: {e}")

        patch_id = _sha(f"{rel_canon}|{new_content}|{time.time()}|{uuid.uuid4()}")
        diff = _unified_diff(before, new_content, label=rel_canon)
        record = {
            "id":            patch_id,
            "target":        rel_canon,
            "description":   description,
            "created":       time.time(),
            "before_sha":    _sha(before),
            "diff":          diff,
            "new_content":   new_content,
        }
        self._patch_path(patch_id).write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        log.info("patch proposed: %s -> %s", patch_id, rel_canon)
        return {k: v for k, v in record.items() if k != "new_content"}

    # ------------------------------------------------------------------
    # approving / rejecting
    # ------------------------------------------------------------------
    def approve(self, patch_id: str) -> dict:
        record = self.get(patch_id)
        target = record.get("target")
        abs_path = _norm_target(
            self.root, target, user_skills_dir=self.user_skills_dir)
        if not abs_path.exists():
            self._patch_path(patch_id).unlink(missing_ok=True)
            raise PatchError(f"{target} no longer exists")
        before = abs_path.read_text(encoding="utf-8")
        if _sha(before) != record.get("before_sha"):
            raise PatchError(
                f"{target} has changed since this patch was proposed; "
                "reject it and ask Jarvis to regenerate.")

        new_content = record["new_content"]
        if abs_path.suffix == ".py":
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                raise PatchError(f"proposed content no longer parses: {e}")

        backup = abs_path.with_suffix(abs_path.suffix + ".bak")
        shutil.copy2(abs_path, backup)
        abs_path.write_text(new_content, encoding="utf-8")

        # Smoke-test Python files via py_compile in a subprocess.
        if abs_path.suffix == ".py":
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(abs_path)],
                    capture_output=True, text=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                shutil.move(str(backup), str(abs_path))
                raise PatchError("py_compile timed out")
            if proc.returncode != 0:
                shutil.move(str(backup), str(abs_path))
                raise PatchError(
                    "py_compile rejected the patch: "
                    + (proc.stderr or proc.stdout).strip())
        backup.unlink(missing_ok=True)

        _git_commit(self.root, [abs_path],
                    f"[jarvis] apply patch {patch_id[:8]}: {record['description']}")
        self._patch_path(patch_id).unlink(missing_ok=True)
        log.info("patch approved & applied: %s", patch_id)
        return {"applied": target, "id": patch_id}

    def reject(self, patch_id: str) -> None:
        self._patch_path(patch_id).unlink(missing_ok=True)
        log.info("patch rejected: %s", patch_id)
