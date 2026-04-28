"""Run local terminal commands with basic safety guardrails."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .base import SkillResult

_DANGEROUS_PAT = re.compile(
    r"\b("
    r"del|erase|rm|rmdir|format|diskpart|shutdown|reboot|"
    r"restart-computer|stop-computer|remove-item|reg\s+delete"
    r")\b",
    re.I,
)


def _clip(text: str, limit: int = 1400) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "\n... [truncated]"


def run(
    command: str,
    *,
    cwd: str | None = None,
    timeout_s: int = 25,
    allow_dangerous: bool = False,
) -> SkillResult:
    cmd = (command or "").strip()
    if not cmd:
        return SkillResult(
            "I need a command string to run.",
            intent="terminal_exec",
            success=False,
        )
    if len(cmd) > 4000:
        return SkillResult(
            "Command is too long.",
            intent="terminal_exec",
            success=False,
        )
    if _DANGEROUS_PAT.search(cmd) and not allow_dangerous:
        return SkillResult(
            "That command looks destructive. Re-run with "
            "allow_dangerous=true if you want me to execute it.",
            intent="terminal_exec",
            success=False,
        )

    wd: Path | None = None
    if cwd:
        wd = Path(cwd).expanduser()
        if not wd.exists() or not wd.is_dir():
            return SkillResult(
                f"Working directory does not exist: {wd}",
                intent="terminal_exec",
                success=False,
            )

    to = max(1, min(int(timeout_s or 25), 300))
    if os.name == "nt":
        argv = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            cmd,
        ]
    else:
        argv = ["/bin/sh", "-lc", cmd]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(wd) if wd is not None else None,
            capture_output=True,
            text=True,
            timeout=to,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return SkillResult(
            f"Command timed out after {to} seconds.",
            intent="terminal_exec",
            success=False,
        )
    except Exception as e:
        return SkillResult(
            f"Command failed to start: {e}",
            intent="terminal_exec",
            success=False,
        )

    stdout = _clip(proc.stdout or "")
    stderr = _clip(proc.stderr or "")
    code = int(proc.returncode)
    if code == 0:
        body = stdout or "(no output)"
        return SkillResult(f"Command succeeded.\n{body}", intent="terminal_exec")
    body = stderr or stdout or "(no output)"
    return SkillResult(
        f"Command failed with exit code {code}.\n{body}",
        intent="terminal_exec",
        success=False,
    )

