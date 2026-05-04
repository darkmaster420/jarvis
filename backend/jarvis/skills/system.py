# backend/jarvis/skills/system.py
"""System-level skills: app management, storage, Docker, etc."""
from __future__ import annotations

import os
import subprocess
import logging
import shutil
import psutil
from pathlib import Path
from typing import Any

from .base import SkillResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App / Folder routing
# ---------------------------------------------------------------------------

def open_app(target: str) -> SkillResult:
    """Open an application, URL, or known folder by name."""
    target_lower = target.lower().strip()

    # Built-in folder mappings (Windows)
    FOLDER_MAP = {
        "games": os.path.expandvars(r"%ProgramFiles%\Games"),
        "documents": os.path.expandvars(r"%USERPROFILE%\Documents"),
        "pictures": os.path.expandvars(r"%USERPROFILE%\Pictures"),
        "music": os.path.expandvars(r"%USERPROFILE%\Music"),
        "videos": os.path.expandvars(r"%USERPROFILE%\Videos"),
        "desktop": os.path.expandvars(r"%USERPROFILE%\Desktop"),
        "downloads": os.path.expandvars(r"%USERPROFILE%\Downloads"),
        "temp": os.path.expandvars(r"%TEMP%"),
        "appdata": os.path.expandvars(r"%APPDATA%"),
        "localappdata": os.path.expandvars(r"%LOCALAPPDATA%"),
    }

    if target_lower in FOLDER_MAP:
        path = FOLDER_MAP[target_lower]
        if os.path.isdir(path):
            try:
                subprocess.run(["explorer.exe", path], check=True, capture_output=True)
                return SkillResult(f"Opened {target} folder.", intent="open_folder", success=True)
            except Exception as e:
                return SkillResult(f"Failed to open {target} folder: {e}", intent="open_folder", success=False)
        else:
            return SkillResult(f"Folder {target!r} not found at {path}.", intent="open_folder", success=False)

    # Try as a direct path first
    if os.path.exists(target):
        if os.path.isdir(target):
            subprocess.run(["explorer.exe", target], check=True, capture_output=True)
            return SkillResult(f"Opened folder {target!r}.", intent="open_folder", success=True)
        else:
            subprocess.run(["explorer.exe", target], check=True, capture_output=True)
            return SkillResult(f"Opened file {target!r}.", intent="open_file", success=True)

    # Try as a URL
    if target_lower.startswith(("http://", "https://")):
        return open_url(target)

    # Try as a known app / start-menu alias
    known_apps = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "powershell": "powershell.exe",
        "task manager": "taskmgr.exe",
        "settings": "ms-settings:",
        "control panel": "control.exe",
        "browser": "msedge.exe",
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "vscode": "code.exe",
        "visual studio code": "code.exe",
        "spotify": "spotify.exe",
        "discord": "discord.exe",
        "slack": "slack.exe",
        "teams": "teams.exe",
        "zoom": "zoom.exe",
        "netflix": "https://www.netflix.com",
        "youtube": "https://www.youtube.com",
        "github": "https://github.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "maps": "https://maps.google.com",
        "weather": "https://weather.com",
        "news": "https://news.google.com",
        "store": "ms-windows-store:",
        "microsoft store": "ms-windows-store:",
        "app store": "ms-windows-store:",
        "game bar": "ms-xbox-gamingoverlay:",
        "xbox": "ms-xbox:",
        "store": "ms-windows-store:",
    }

    if target_lower in known_apps:
        app = known_apps[target_lower]
        try:
            subprocess.Popen([app], shell=True)
            return SkillResult(f"Opened {target}.", intent="open_app", success=True)
        except Exception as e:
            return SkillResult(f"Failed to open {target}: {e}", intent="open_app", success=False)

    # Fallback: try to launch via start / cmd
    try:
        subprocess.Popen(["start", target], shell=True)
        return SkillResult(f"Attempted to open {target!r}.", intent="open_app", success=True)
    except Exception as e:
        return SkillResult(f"Could not open {target!r}: {e}", intent="open_app", success=False)


def close_app(target: str) -> SkillResult:
    """Close an application by name or process."""
    target_lower = target.lower().strip()
    try:
        # Try exact process name match
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == target_lower:
                proc.kill()
                return SkillResult(f"Closed {target!r}.", intent="close_app", success=True)
        # Try partial match
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and target_lower in proc.info["name"].lower():
                proc.kill()
                return SkillResult(f"Closed {target!r}.", intent="close_app", success=True)
        return SkillResult(f"Could not find {target!r} to close.", intent="close_app", success=False)
    except Exception as e:
        return SkillResult(f"Error closing {target!r}: {e}", intent="close_app", success=False)


def start_mongodb_container() -> SkillResult:
    """Start the MongoDB Docker container if available."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=mongodb", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        )
        if "mongodb" in result.stdout:
            subprocess.run(["docker", "start", "mongodb"], check=True, capture_output=True)
            return SkillResult("Started MongoDB container.", intent="start_mongodb_container", success=True)
        else:
            subprocess.run(["docker", "run", "-d", "--name", "mongodb", "-p", "27017:27017", "mongo"], check=True, capture_output=True)
            return SkillResult("Created and started MongoDB container.", intent="start_mongodb_container", success=True)
    except FileNotFoundError:
        return SkillResult("Docker is not installed or not in PATH.", intent="start_mongodb_container", success=False)
    except subprocess.CalledProcessError as e:
        return SkillResult(f"Failed to start MongoDB: {e}", intent="start_mongodb_container", success=False)


# ---------------------------------------------------------------------------
# System info helpers
# ---------------------------------------------------------------------------

def get_system_stats() -> SkillResult:
    """Return basic system statistics."""
    try:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return SkillResult(
            f"CPU: {cpu}%, RAM: {mem.percent}%, Disk: {disk.percent}% used.",
            intent="sys_stats",
            success=True,
        )
    except Exception as e:
        return SkillResult(f"Failed to get system stats: {e}", intent="sys_stats", success=False)


# ---------------------------------------------------------------------------
# Start menu / alias helpers
# ---------------------------------------------------------------------------

_alias_lookup: dict[str, str] = {}


def set_alias_lookup(lookup: dict[str, str]) -> None:
    """Inject the start-menu alias cache."""
    global _alias_lookup
    _alias_lookup = lookup


def prewarm_start_menu_cache() -> None:
    """Preload start-menu apps into _alias_lookup."""
    try:
        import winreg
        keys = [
            winreg.HKEY_CURRENT_USER,
            winreg.HKEY_LOCAL_MACHINE,
        ]
        for root in keys:
            try:
                with winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as hkey:
                    desktop = winreg.QueryValueEx(hkey, "Common Programs")[0]
                    apps_dir = os.path.join(desktop, "Start Menu", "Programs")
                    if os.path.isdir(apps_dir):
                        for entry in os.listdir(apps_dir):
                            path = os.path.join(apps_dir, entry)
                            if os.path.isdir(path):
                                for sub in os.listdir(path):
                                    sub_path = os.path.join(path, sub)
                                    if sub_path.endswith(".lnk"):
                                        name = os.path.splitext(sub)[0].lower()
                                        _alias_lookup[name] = sub_path
            except Exception:
                pass
    except Exception as e:
        log.warning("Failed to prewarm start menu cache: %s", e)
