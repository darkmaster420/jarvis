"""System-control skills (Windows-first)."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Callable

from .base import SkillResult

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"


_VolTriple = tuple[Callable[[], float], Callable[[float], None], Callable[[bool], None]]


def _win_volume() -> _VolTriple | None:
    if not IS_WIN:
        return None
    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as e:  # pragma: no cover
        log.warning("pycaw unavailable: %s", e)
        return None

    try:
        devices = AudioUtilities.GetSpeakers()
        raw = getattr(devices, "_dev", None) or getattr(devices, "dev", None) or devices
        iface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
    except Exception as e:
        log.warning("pycaw init failed: %s", e)
        return None

    def get() -> float:
        return float(vol.GetMasterVolumeLevelScalar())

    def setv(x: float) -> None:
        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, x)), None)

    def mute(m: bool) -> None:
        vol.SetMute(1 if m else 0, None)

    return get, setv, mute


_VOL: _VolTriple | None = None


def _vol() -> _VolTriple | None:
    global _VOL
    if _VOL is None:
        _VOL = _win_volume()
    return _VOL


def volume(change: str, amount: float = 0.1) -> SkillResult:
    v = _vol()
    if v is None:
        return SkillResult("Volume control is only supported on Windows.",
                           intent="volume", success=False)
    get, setv, mute = v
    cur = get()
    if change == "up":
        setv(cur + amount)
        return SkillResult(f"Volume is now {int((cur + amount) * 100)} percent.",
                           intent="volume")
    if change == "down":
        setv(cur - amount)
        return SkillResult(f"Volume is now {max(0, int((cur - amount) * 100))} percent.",
                           intent="volume")
    if change == "mute":
        mute(True)
        return SkillResult("Muted.", intent="volume")
    if change == "unmute":
        mute(False)
        return SkillResult("Unmuted.", intent="volume")
    if change == "set":
        setv(amount)
        return SkillResult(f"Volume set to {int(amount * 100)} percent.", intent="volume")
    return SkillResult("I didn't catch that volume command.",
                       intent="volume", success=False)


APP_ALIASES: dict[str, str] = {
    "file explorer":      "explorer.exe",
    "files":              "explorer.exe",
    "explorer":           "explorer.exe",
    "my computer":        "explorer.exe",
    "this pc":            "explorer.exe",

    "notepad":            "notepad.exe",
    "calculator":         "calc.exe",
    "calc":               "calc.exe",
    "paint":              "mspaint.exe",
    "task manager":       "taskmgr.exe",
    "cmd":                "cmd.exe",
    "command prompt":     "cmd.exe",
    "terminal":           "wt.exe",
    "windows terminal":   "wt.exe",
    "powershell":         "powershell.exe",
    "control panel":      "control.exe",
    "registry editor":    "regedit.exe",
    "regedit":            "regedit.exe",

    "settings":           "ms-settings:",
    "bluetooth settings": "ms-settings:bluetooth",
    "display settings":   "ms-settings:display",
    "sound settings":     "ms-settings:sound",
    "wifi settings":      "ms-settings:network-wifi",

    "browser":            "msedge.exe",
    "edge":               "msedge.exe",
    "microsoft edge":     "msedge.exe",
    "chrome":             "chrome.exe",
    "google chrome":      "chrome.exe",
    "firefox":            "firefox.exe",
    "brave":              "brave.exe",

    # Common web — opens in default browser (skips Get-StartApps entirely)
    "youtube":            "https://www.youtube.com",
    "you tube":           "https://www.youtube.com",
    "yt":                 "https://www.youtube.com",
    "github":             "https://github.com",
    "twitch":             "https://www.twitch.tv",
    "netflix":            "https://www.netflix.com",
    "gmail":              "https://mail.google.com",
    "reddit":             "https://www.reddit.com",
    "twitter":            "https://twitter.com",
    "x twitter":          "https://twitter.com",
    "google":             "https://www.google.com",

    "spotify":            "spotify:",
    "discord":            "discord://",
    "steam":              "steam://open/main",
    "steam library":      "steam://open/games",
    "epic games":         "com.epicgames.launcher://",
    "epic":               "com.epicgames.launcher://",
    "obs":                "obs64.exe",
    "vscode":             "code.exe",
    "vs code":            "code.exe",
    "visual studio code": "code.exe",
    "cursor":             "cursor.exe",
    "zoom":               "zoommtg://",
    "teams":              "msteams:",
    "slack":              "slack:",
    "whatsapp":           "whatsapp:",
}


_dynamic_alias_lookup: "Callable[[str], str | None] | None" = None


def set_alias_lookup(fn: "Callable[[str], str | None] | None") -> None:
    """Register a callback (phrase -> target|None) that `open_app` should
    consult before falling back to the built-in alias table. Used by the
    Memory subsystem so users can teach Jarvis custom launch shortcuts."""
    global _dynamic_alias_lookup
    _dynamic_alias_lookup = fn


def _resolve_app(name: str) -> str:
    key = name.strip().lower().rstrip("?.! ")
    if _dynamic_alias_lookup is not None:
        try:
            learned = _dynamic_alias_lookup(key)
            if learned:
                return learned
        except Exception as e:
            log.warning("dynamic alias lookup failed for %r: %s", key, e)
    if key in APP_ALIASES:
        return APP_ALIASES[key]
    k2 = key.removesuffix(".exe")
    if k2 in APP_ALIASES:
        return APP_ALIASES[k2]
    return name.strip()


_START_APPS_CACHE: list[tuple[str, str]] | None = None
_START_APPS_LOAD_LOCK = threading.Lock()


def _load_start_apps() -> list[tuple[str, str]]:
    """Query Windows Start Menu for all installed apps. Cached for the
    process lifetime; the first run can take several seconds. Thread-safe
    and does not set the cache to an empty list until loading finishes
    (avoids a race where a second open_app would see an empty list)."""
    global _START_APPS_CACHE
    if _START_APPS_CACHE is not None:
        return _START_APPS_CACHE
    with _START_APPS_LOAD_LOCK:
        if _START_APPS_CACHE is not None:
            return _START_APPS_CACHE
        rows: list[tuple[str, str]] = []
        if not IS_WIN:
            _START_APPS_CACHE = rows
            return rows
        try:
            import json as _json

            # Lighter than ConvertTo-Json for huge start menus; still valid JSON.
            ps = (
                "Get-StartApps | Select-Object Name,AppID | "
                "ConvertTo-Json -Compress -Depth 2"
            )
            proc = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-NoLogo",
                    "-Command", ps,
                ],
                capture_output=True, text=True, timeout=8,
                creationflags=0x08000000,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                _START_APPS_CACHE = rows
                return _START_APPS_CACHE
            data = _json.loads(proc.stdout)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                nm = (item.get("Name") or "").strip()
                aid = (item.get("AppID") or "").strip()
                if nm and aid:
                    rows.append((nm.lower(), aid))
        except Exception as e:
            log.warning("Get-StartApps failed: %s", e)
        _START_APPS_CACHE = rows
        if rows:
            log.info("Start Menu app list loaded (%d entries).", len(rows))
    return _START_APPS_CACHE


def prewarm_start_menu_cache() -> None:
    """Call once in a background thread at startup so the first \"open\"
    command does not block on Get-StartApps."""
    if IS_WIN:
        try:
            _load_start_apps()
        except Exception as e:  # pragma: no cover
            log.warning("prewarm start menu failed: %s", e)


def _find_start_app(name: str) -> tuple[str, str] | None:
    """Find the best Start Menu app matching `name`. Returns (display, AppID)
    or None."""
    needle = name.strip().lower()
    if not needle:
        return None
    apps = _load_start_apps()
    best: tuple[int, str, str] | None = None  # (score, display, AppID)
    for disp, aid in apps:
        score: int | None = None
        if disp == needle:
            score = 0
        elif disp.startswith(needle + " ") or disp.startswith(needle + ":"):
            score = 1
        elif needle in disp.split():
            score = 2
        elif needle in disp:
            score = 3
        if score is not None and (best is None or score < best[0]):
            best = (score, disp, aid)
    return (best[1], best[2]) if best else None


def _launch(target: str) -> None:
    """Fire-and-forget open via ``ShellExecute`` (faster and lighter than
    ``cmd /c start`` for browsers, ``https:``, and protocol handlers)."""
    if not IS_WIN:
        subprocess.Popen([target])
        return
    t = (target or "").strip()
    if not t:
        return
    import ctypes

    rc = int(ctypes.windll.shell32.ShellExecuteW(
        None, "open", t, None, None, 1))  # SW_SHOWNORMAL
    if rc <= 32:
        raise OSError(f"ShellExecuteW failed, code {rc}")


def _launch_appid(app_id: str) -> None:
    """Launch a UWP/Start Menu app by its AppUserModelID."""
    if not IS_WIN:
        return
    subprocess.Popen(
        ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
        shell=False, creationflags=0x08000000,
    )


def open_app(name: str) -> SkillResult:
    original = (name or "").strip()
    if not original:
        return SkillResult("What should I open?", intent="open_app", success=False)
    target = _resolve_app(original)
    is_alias = target != original
    is_uri   = "://" in target or target.endswith(":")

    # Exe / URL / memory alias: single open, no Get-StartApps.
    if is_uri or is_alias:
        try:
            _launch(target)
        except Exception as e:
            log.warning("open_app launch failed: target=%r err=%s", target, e)
        label = original if is_alias else target
        return SkillResult(f"Opening {label}.", intent="open_app")

    # Bare name on Windows: resolve Start list first (uses cached list when
    # prewarmed) so we never double-launch a useless `start "" name` and then
    # AppsFolder, and we avoid a redundant ShellExecute when the UWP id wins.
    if IS_WIN:
        match = _find_start_app(original)
        if match is not None:
            disp, aid = match
            try:
                _launch_appid(aid)
                return SkillResult(f"Opening {disp}.", intent="open_app")
            except Exception as e:
                log.warning("AppsFolder launch failed: aid=%r err=%s", aid, e)
    try:
        _launch(target)
    except Exception as e:
        log.warning("open_app launch failed: target=%r err=%s", target, e)
    return SkillResult(f"Opening {target}.", intent="open_app")


# Process names that should never be touched - closing these would take the
# desktop session down with them.
_PROTECTED_PROCS: frozenset[str] = frozenset({
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "taskmgr.exe", "python.exe", "pythonw.exe",
    "jarvis.exe", "jarvis_hud.exe", "ollama.exe", "ollama app.exe",
})


def _target_to_procnames(target: str) -> list[str]:
    """Turn whatever `open_app` would launch into the exe basenames we'd
    expect to find in the process list. Handles exes, URIs, AppsFolder ids,
    and bare names."""
    t = (target or "").strip()
    if not t:
        return []
    # URI / UWP AppId - strip scheme or !AppId suffix, best we can do
    if "://" in t:
        t = t.split("://", 1)[0]
    if t.endswith(":"):
        t = t[:-1]
    t = t.split("!", 1)[0]
    # Some aliases are just a command like 'explorer shell:bluetooth' - take
    # the first token as the process name.
    t = t.split()[0] if " " in t else t
    t = os.path.basename(t).lower()
    candidates = {t}
    if not t.endswith(".exe"):
        candidates.add(t + ".exe")
    return sorted(candidates)


def close_app(name: str) -> SkillResult:
    """Terminate processes whose image name matches `name` (or any alias
    the user has taught us). Substring match on the exe basename so
    'chrome' hits 'chrome.exe'."""
    original = (name or "").strip()
    if not original:
        return SkillResult("What should I close?",
                           intent="close_app", success=False)
    if not IS_WIN:
        return SkillResult("Close is only supported on Windows.",
                           intent="close_app", success=False)
    try:
        import psutil
    except ImportError:
        return SkillResult("psutil isn't installed; can't close apps.",
                           intent="close_app", success=False)

    target = _resolve_app(original)
    tnorm = (target or "").strip()
    if tnorm.lower().startswith(("http://", "https://")):
        # "close YouTube" etc. — site runs inside the browser, not a "youtube" process
        from . import web
        r = web.close_browser_tab()
        return SkillResult(
            r.reply or "Done.",
            intent="close_browser_tab",
            success=bool(r.success),
        )
    needles = _target_to_procnames(target)
    # Also allow the raw user-facing name (covers "close discord" when
    # discord isn't in our alias map).
    needles.append(original.lower())
    needles.extend(original.lower().split())
    needles = [n for n in {n for n in needles if len(n) >= 3}
               if n not in _PROTECTED_PROCS]

    killed: list[str] = []
    tried  = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if not pname or pname in _PROTECTED_PROCS:
            continue
        if not any(n == pname or n in pname for n in needles):
            continue
        tried += 1
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                proc.kill()
            killed.append(pname)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as e:
            log.warning("close_app: failed to terminate %s: %s", pname, e)

    if killed:
        uniq = sorted(set(killed))
        label = original if _resolve_app(original) != original else uniq[0]
        return SkillResult(
            f"Closed {label}." if len(uniq) == 1 else
            f"Closed {len(killed)} processes matching {original}.",
            intent="close_app")
    if tried:
        return SkillResult(
            f"Found {original} but couldn't close it (access denied).",
            intent="close_app", success=False)
    return SkillResult(f"I don't see {original} running.",
                       intent="close_app", success=False)


def lock() -> SkillResult:
    if IS_WIN:
        try:
            import ctypes

            ctypes.windll.user32.LockWorkStation()
            return SkillResult("Locking the workstation.", intent="lock")
        except Exception as e:  # pragma: no cover
            return SkillResult(f"Couldn't lock: {e}", intent="lock", success=False)
    return SkillResult("Lock is only supported on Windows.", intent="lock", success=False)


def shutdown(delay_s: int = 10) -> SkillResult:
    if IS_WIN:
        try:
            subprocess.Popen(["shutdown", "/s", "/t", str(delay_s)])
            return SkillResult(f"Shutting down in {delay_s} seconds.", intent="shutdown")
        except Exception as e:
            return SkillResult(f"Shutdown failed: {e}", intent="shutdown", success=False)
    try:
        os.system(f"shutdown -h +{max(1, delay_s // 60)}")
        return SkillResult("Scheduled shutdown.", intent="shutdown")
    except Exception as e:
        return SkillResult(f"Shutdown failed: {e}", intent="shutdown", success=False)


def sleep_pc() -> SkillResult:
    if IS_WIN:
        try:
            subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"]
            )
            return SkillResult("Going to sleep.", intent="sleep")
        except Exception as e:
            return SkillResult(f"Sleep failed: {e}", intent="sleep", success=False)
    return SkillResult("Sleep is only supported on Windows.", intent="sleep", success=False)


def cancel_shutdown() -> SkillResult:
    if IS_WIN:
        try:
            subprocess.Popen(["shutdown", "/a"])
            return SkillResult("Shutdown cancelled.", intent="cancel_shutdown")
        except Exception as e:
            return SkillResult(f"Cancel failed: {e}",
                               intent="cancel_shutdown", success=False)
    return SkillResult("Only supported on Windows.",
                       intent="cancel_shutdown", success=False)
