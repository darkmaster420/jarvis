"""First-run / prod bootstrapping: optional pip installs and Ollama model pulls.

* Python: if desktop automation packages are missing, install them in-process
  (``pip`` must be available; idempotent for future starts).
* Ollama: for each configured ``llm.vision_model`` and ``llm.model`` not yet
  present locally, runs ``Client.pull`` in a background thread so the server
  starts immediately while large downloads continue. After pulls, sends a
  minimal chat/generate to each model so the runner loads weights before the
  first user utterance (same thread as pulls; HUD ``ollama.ready`` follows).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable

log = logging.getLogger(__name__)

_DESKTOP_PIP = ("mss>=9.0.0", "Pillow>=10.0.0", "pyautogui>=0.9.54")


def _can_import_desktop() -> bool:
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
        import pyautogui  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_desktop_pip() -> None:
    """Install mss, Pillow, pyautogui if not importable."""
    if _can_import_desktop():
        return
    if not sys.executable:
        return
    log.info(
        "Installing desktop automation packages (one-time): %s",
        ", ".join(_DESKTOP_PIP),
    )
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "-q", *_DESKTOP_PIP],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if p.returncode != 0:
            log.warning("pip install failed (rc=%s): %s",
                        p.returncode, (p.stderr or p.stdout)[:2000])
            return
    except subprocess.TimeoutExpired:
        log.warning("pip install timed out after 10 min")
        return
    except FileNotFoundError:
        log.warning("pip not found; run: pip install -r backend/requirements.txt")
        return
    except Exception as e:  # pragma: no cover
        log.warning("pip install: %s", e)
        return
    if not _can_import_desktop():
        log.warning("Desktop deps still missing after pip; restart or install manually.")
        return
    log.info("Desktop packages ready: mss, Pillow, pyautogui.")


def ensure_openwakeword_models() -> None:
    """Download built-in openWakeWord model files if the wheel lacks them."""
    try:
        from openwakeword.utils import download_models
    except ImportError:
        log.debug("openwakeword missing; skip wake word model bootstrap")
        return
    try:
        download_models()
    except Exception as e:
        log.warning("openWakeWord model bootstrap failed: %s", e)


def _ollama_api_up(host: str, timeout_s: float) -> bool:
    """True if the Ollama HTTP server answers ``GET /api/tags``."""
    h = (host or "").rstrip("/") or "http://127.0.0.1:11434"
    if not h.startswith("http"):
        h = f"http://{h.lstrip()}"
    url = f"{h}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _is_loopback_ollama_host(host: str) -> bool:
    raw = (host or "").strip() or "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = f"http://{raw}"
    u = urllib.parse.urlparse(raw)
    h = (u.hostname or "127.0.0.1").lower()
    return h in ("127.0.0.1", "localhost", "::1")


def _windows_ollama_executable(cfg) -> str | None:
    """Path to the installed Ollama app, or None."""
    custom = (getattr(cfg.llm, "ollama_app_path", None) or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    pfx = os.environ.get("ProgramFiles(x86)", "")
    for base in (
        os.path.join(la, "Programs", "Ollama") if la else "",
        os.path.join(pf, "Ollama") if pf else "",
        os.path.join(pfx, "Ollama") if pfx else "",
    ):
        if not base:
            continue
        for name in ("Ollama.exe", "ollama app.exe", "ollama.exe"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return None


def _win_start_ollama_minimized(executable: str) -> bool:
    """Start the Ollama GUI minimized (typically ends up in the notification tray)."""
    try:
        import ctypes
    except ImportError:  # pragma: no cover
        return False
    # SW_SHOWMINNOACTIVE: minimized, does not activate — tray-style startup.
    sw_min_no_active = 7
    rc = int(ctypes.windll.shell32.ShellExecuteW(
        None, "open", executable, None, None, sw_min_no_active,
    ))
    if rc <= 32:
        log.warning("Could not start Ollama (ShellExecuteW %s): %s", rc, executable)
        return False
    return True


def _maybe_autostart_ollama_windows(cfg, host: str) -> None:
    if sys.platform != "win32" or not _is_loopback_ollama_host(host):
        return
    if not bool(getattr(cfg.llm, "auto_start_ollama", True)):
        return
    if (getattr(cfg.llm, "provider", "ollama") or "ollama").lower() != "ollama":
        return
    if _ollama_api_up(host, 0.5):
        return
    exe = _windows_ollama_executable(cfg)
    if not exe:
        log.warning(
            "Ollama is not running and no Ollama app was found. "
            "Install from https://ollama.com/download or set llm.ollama_app_path.",
        )
        return
    log.info("Ollama not listening — starting %s minimized (tray).", exe)
    if not _win_start_ollama_minimized(exe):
        return
    t0 = time.monotonic()
    while time.monotonic() - t0 < 90.0:
        if _ollama_api_up(host, 1.0):
            log.info("Ollama is responding at %s", host)
            return
        time.sleep(0.5)
    log.warning("Ollama was started but did not become ready at %s within 90s.", host)


def ensure_ollama_models(cfg) -> None:
    """Pull ``vision_model`` and ``llm.model`` if not already on disk. Blocking;
    run from a worker thread. Uses the Ollama HTTP API (same as Jarvis, no
    separate ``ollama`` CLI on PATH)."""
    try:
        import ollama
    except ImportError:
        log.debug("ollama package missing; skip model bootstrap")
        return
    host = (getattr(cfg.llm, "host", None) or "http://127.0.0.1:11434").rstrip("/")
    _maybe_autostart_ollama_windows(cfg, host)
    client = ollama.Client(host=host)
    try:
        lr = client.list()
    except Exception as e:
        log.info("Ollama at %s not reachable — auto-pull skipped (start Ollama first): %s",
                 host, e)
        return
    have = {m.model.lower() for m in (getattr(lr, "models", None) or [])}
    # Vision first (usually smaller) then main chat model; deduped
    want: "OrderedDict[str, None]" = OrderedDict()
    vm = (getattr(cfg.llm, "vision_model", None) or "").strip()
    if vm:
        want[vm] = None
    for fallback in getattr(cfg.llm, "vision_fallback_models", []) or []:
        fb = str(fallback).strip()
        if fb and fb not in want:
            want[fb] = None
    mm = (getattr(cfg.llm, "model", None) or "").strip()
    if mm and mm not in want:
        want[mm] = None
    for name in want:
        if name.lower() in have:
            log.debug("Ollama model already available: %s", name)
            continue
        log.info("Pulling Ollama model (first run; may be large) — %s", name)
        try:
            _stream_pull(client, name)
        except Exception as e:
            log.warning("Ollama pull %s failed: %s (try: ollama pull %s)",
                        name, e, name)
            continue
        have.add(name.lower())
        log.info("Ollama model ready: %s", name)

    _warm_models_in_order(client, cfg, want)


def _stream_pull(client, name: str) -> None:
    """Run pull with streaming; fall back for older ollama-python APIs."""
    try:
        stream = client.pull(name, stream=True)
    except TypeError:
        try:
            client.pull(name)  # non-streaming
        except Exception as e2:
            log.warning("Ollama pull %s: %s", name, e2)
        return
    if stream is None or isinstance(stream, (str, bytes, dict)):
        log.info("Ollama pull %s: %s", name, str(stream)[:200])
        return
    last_log = None
    try:
        for part in stream:
            line = _pull_chunk_line(part)
            if line and line != last_log:
                last_log = line
                log.info("  ollama pull %s: %s", name, line[:200])
    except TypeError:
        return


def _pull_chunk_line(part) -> str:
    if isinstance(part, dict):
        return part.get("status", "") or part.get("error", "")
    o = getattr(part, "status", None) or getattr(part, "error", None)
    return (str(o) if o is not None else str(part) or "")


def _warm_ollama_model(client, name: str) -> None:
    """Load the model into the Ollama runner with a tiny request (first use can be
    very slow; this runs at startup so the user’s first real prompt is not stuck
    on cold load). Tries :py:meth:`Client.chat` then :py:meth:`Client.generate`."""
    n = (name or "").strip()
    if not n:
        return
    opts = {"num_predict": 2}
    try:
        client.chat(
            model=n,
            messages=[{"role": "user", "content": "ping"}],
            stream=False,
            options=opts,
        )
        return
    except Exception as e1:
        log.debug("Ollama chat warm for %s: %s", n, e1)
    try:
        client.generate(model=n, prompt="ok", stream=False, options=opts)
    except Exception as e2:
        log.warning("Ollama warm failed for %s: %s", n, e2)


def _warm_models_in_order(
    client,
    cfg,
    want: "OrderedDict[str, None]",
) -> None:
    """Main chat model first, then the rest of *want* (vision / fallbacks)."""
    if (getattr(cfg.llm, "provider", "ollama") or "ollama").lower() != "ollama":
        return
    order: list[str] = []
    mm = (getattr(cfg.llm, "model", None) or "").strip()
    if mm:
        order.append(mm)
    for k in want:
        s = (k or "").strip()
        if s and s not in order:
            order.append(s)
    for name in order:
        log.info("Ollama warm: loading %s into memory (quick test)…", name)
        t0 = time.monotonic()
        _warm_ollama_model(client, name)
        log.info("Ollama warm: done %s in %.1fs", name, time.monotonic() - t0)


def start_ollama_bootstrap_thread(
    cfg,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run ``ensure_ollama_models`` in a daemon thread. *on_complete* is called
    from that thread when finished (success, partial, or unreachable Ollama)."""

    def _run() -> None:
        try:
            ensure_ollama_models(cfg)
        finally:
            if on_complete is not None:
                on_complete()

    t = threading.Thread(target=_run, name="ollama-bootstrap", daemon=True)
    t.start()
