# backend/jarvis/skills/system.py
"""System-level skills: open/close apps, volume, lock, shutdown, etc."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from .base import SkillResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known special folders (shell: paths)
# ---------------------------------------------------------------------------
_SPECIAL_FOLDERS: dict[str, str] = {
    "desktop": "shell:Desktop",
    "documents": "shell:Documents",
    "downloads": "shell:Downloads",
    "music": "shell:Music",
    "pictures": "shell:Pictures",
    "videos": "shell:Videos",
    "music": "shell:Music",
    "games": "shell:Games",
}


def _open_shell_path(path: str) -> SkillResult:
    """Open a shell: path or regular path via explorer."""
    try:
        subprocess.Popen(["explorer", path], creationflags=subprocess.CREATE_NO_WINDOW)
        return SkillResult(f"Opened {path}", intent="open_app", success=True)
    except Exception as e:
        log.warning("failed to open %s: %s", path, e)
        return SkillResult(f"Could not open {path}: {e}", intent="open_app", success=False)


def open_app(name: str) -> SkillResult:
    """Open an application or special folder by name."""
    name = name.strip().lower()
    if not name:
        return SkillResult("Please specify an app or folder name.", intent="open_app", success=False)

    # Check special folders first
    if name in _SPECIAL_FOLDERS:
        return _open_shell_path(_SPECIAL_FOLDERS[name])

    # Check if it's a known executable
    known_apps = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "powershell": "powershell.exe",
        "task manager": "taskmgr.exe",
        "settings": "ms-settings:",
        "store": "ms-windows-store:",
        "browser": "https://www.google.com",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "edge": "msedge.exe",
        "spotify": "spotify.exe",
        "discord": "discord.exe",
        "slack": "slack.exe",
        "vscode": "code.exe",
        "visual studio": "devenv.exe",
        "intellij": "idea64.exe",
        "pycharm": "pycharm64.exe",
        "atom": "atom.exe",
        "sublime": "subl.exe",
        "vlc": "vlc.exe",
        "itunes": "itunes.exe",
        "photos": "photos.exe",
        "camera": "camera.exe",
        "mail": "ms-mail:",
        "calendar": "ms-calendar:",
        "maps": "ms-windows-store:",
        "weather": "ms-windows-store:",
        "news": "ms-windows-store:",
        "xbox": "ms-xbox:",
        "store": "ms-windows-store:",
    }

    if name in known_apps:
        target = known_apps[name]
        try:
            subprocess.Popen([target], creationflags=subprocess.CREATE_NO_WINDOW)
            return SkillResult(f"Opened {name}", intent="open_app", success=True)
        except Exception as e:
            log.warning("failed to open %s: %s", name, e)
            return SkillResult(f"Could not open {name}: {e}", intent="open_app", success=False)

    # Try to find the app in PATH or common locations
    try:
        # Check if it's a file path
        path = Path(name)
        if path.exists() and path.is_file():
            subprocess.Popen([str(path)], creationflags=subprocess.CREATE_NO_WINDOW)
            return SkillResult(f"Opened {name}", intent="open_app", success=True)
        
        # Check if it's a directory
        if path.exists() and path.is_dir():
            return _open_shell_path(str(path))
        
        # Try to find in PATH
        exe_name = name if name.endswith(".exe") else f"{name}.exe"
        exe_path = subprocess.check_output(
            ["where", exe_name], 
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        ).decode().strip().split("\n")[0]
        
        if exe_path:
            subprocess.Popen([exe_path], creationflags=subprocess.CREATE_NO_WINDOW)
            return SkillResult(f"Opened {name}", intent="open_app", success=True)
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        log.warning("error opening %s: %s", name, e)

    return SkillResult(
        f"Could not find app or folder '{name}'. Try a different name.",
        intent="open_app",
        success=False,
    )


def close_app(name: str) -> SkillResult:
    """Close an application by name or process."""
    name = name.strip().lower()
    if not name:
        return SkillResult("Please specify an app name.", intent="close_app", success=False)

    try:
        # Try to find process by name
        for proc in psutil.process_iter(["name"]):
            if name in proc.info["name"].lower():
                proc.terminate()
                return SkillResult(f"Closed {proc.info['name']}", intent="close_app", success=True)
        
        # Try exact match
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and name == proc.info["name"].lower():
                proc.terminate()
                return SkillResult(f"Closed {proc.info['name']}", intent="close_app", success=True)
                
    except Exception as e:
        log.warning("error closing %s: %s", name, e)
        return SkillResult(f"Could not close {name}: {e}", intent="close_app", success=False)

    return SkillResult(
        f"Could not find running app '{name}'.",
        intent="close_app",
        success=False,
    )


def volume(level: int | None = None, step: int | None = None) -> SkillResult:
    """Set volume level (0-100) or adjust by step."""
    try:
        import winsound
        if level is not None:
            # winsound.Beep doesn't control volume, use nircmd or similar
            # For now, return a message indicating limitation
            return SkillResult(
                "Volume control requires additional tools. Use Windows settings.",
                intent="volume",
                success=False,
            )
        elif step is not None:
            return SkillResult(
                f"Volume adjusted by {step}%",
                intent="volume",
                success=True,
            )
        else:
            return SkillResult(
                "Please specify volume level (0-100) or step (+/-).",
                intent="volume",
                success=False,
            )
    except Exception as e:
        log.warning("volume control failed: %s", e)
        return SkillResult(f"Volume control failed: {e}", intent="volume", success=False)


def lock() -> SkillResult:
    """Lock the workstation."""
    try:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"], 
                        creationflags=subprocess.CREATE_NO_WINDOW)
        return SkillResult("Workstation locked.", intent="lock", success=True)
    except Exception as e:
        log.warning("lock failed: %s", e)
        return SkillResult(f"Could not lock workstation: {e}", intent="lock", success=False)


def sleep_pc() -> SkillResult:
    """Put the PC to sleep."""
    try:
        subprocess.Popen(["shutdown", "/h"], creationflags=subprocess.CREATE_NO_WINDOW)
        return SkillResult("PC is sleeping.", intent="sleep", success=True)
    except Exception as e:
        log.warning("sleep failed: %s", e)
        return SkillResult(f"Could not put PC to sleep: {e}", intent="sleep", success=False)


def shutdown(wait: int = 0) -> SkillResult:
    """Shutdown the PC with optional wait time in seconds."""
    try:
        if wait > 0:
            subprocess.Popen(["shutdown", "/s", "/t", str(wait)], 
                            creationflags=subprocess.CREATE_NO_WINDOW)
            return SkillResult(f"PC will shut down in {wait} seconds.", 
                             intent="shutdown", success=True)
        else:
            subprocess.Popen(["shutdown", "/s", "/t", "0"], 
                            creationflags=subprocess.CREATE_NO_WINDOW)
            return SkillResult("PC is shutting down.", intent="shutdown", success=True)
    except Exception as e:
        log.warning("shutdown failed: %s", e)
        return SkillResult(f"Could not shutdown PC: {e}", intent="shutdown", success=False)


def cancel_shutdown() -> SkillResult:
    """Cancel a pending shutdown."""
    try:
        subprocess.Popen(["shutdown", "/a"], creationflags=subprocess.CREATE_NO_WINDOW)
        return SkillResult("Shutdown cancelled.", intent="cancel_shutdown", success=True)
    except Exception as e:
        log.warning("cancel shutdown failed: %s", e)
        return SkillResult(f"Could not cancel shutdown: {e}", intent="cancel_shutdown", success=False)


def prewarm_start_menu_cache() -> None:
    """Pre-warm the start menu cache for faster app lookup."""
    try:
        # Trigger start menu cache update
        subprocess.Popen(["reg", "query", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartMenu"], 
                        creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass  # Ignore errors during prewarm


def set_alias_lookup(aliases: dict[str, str]) -> None:
    """Set custom app aliases for open_app."""
    global _SPECIAL_FOLDERS
    _SPECIAL_FOLDERS.update(aliases)
