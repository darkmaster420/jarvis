"""Entry point for the Jarvis backend."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml  # noqa: F401  # PyYAML from backend/requirements.txt
except ModuleNotFoundError as e:
    be = Path(__file__).resolve().parent.parent
    vpy = be / ".venv" / "Scripts" / "python.exe"
    req = be / "requirements.txt"
    sys.stderr.write(
        "Missing the 'yaml' module (install PyYAML). The backend should use the project venv:\n"
        f"  {vpy} -m pip install -r {req}\n"
        f"Then run with the same interpreter, e.g.:\n"
        f"  {vpy} -m jarvis.main\n"
    )
    raise SystemExit(1) from e

import argparse
import asyncio
import logging
import os
import subprocess
import signal
import time
from .config import Config, _default_install_root

log = logging.getLogger(__name__)


_RELOAD_EXTS = {
    ".py", ".yaml", ".yml", ".json", ".toml", ".ini", ".md",
    ".txt", ".cmake", ".iss", ".ps1", ".bat",
}
_RELOAD_IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "node_modules", "build", "dist", ".cursor", "agent-transcripts",
}


def _iter_watch_files(root: Path):
    root = root.resolve()
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _RELOAD_IGNORED_DIRS]
        pcur = Path(cur)
        for fn in files:
            p = pcur / fn
            if p.suffix.lower() not in _RELOAD_EXTS:
                continue
            yield p


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for p in _iter_watch_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


def _strip_reload_flags(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dev-reload":
            i += 1
            continue
        if a in ("--reload-interval",):
            i += 2
            continue
        if a.startswith("--reload-interval="):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _run_with_reloader(raw_argv: list[str], interval_s: float, root: Path) -> int:
    watch_root = root.resolve()
    interval_s = max(0.2, float(interval_s or 0.8))
    child_argv = _strip_reload_flags(raw_argv)
    cmd = [sys.executable, "-m", "jarvis.main", *child_argv]
    env = os.environ.copy()
    env["JARVIS_RELOAD_CHILD"] = "1"
    env["PYTHONUNBUFFERED"] = env.get("PYTHONUNBUFFERED", "1")

    log.info("dev reload watching: %s", watch_root)
    log.info("dev reload interval: %.2fs", interval_s)

    snap = _snapshot_tree(watch_root)
    child = subprocess.Popen(cmd, env=env)
    try:
        while True:
            time.sleep(interval_s)
            if child.poll() is not None:
                return int(child.returncode or 0)
            new_snap = _snapshot_tree(watch_root)
            if new_snap == snap:
                continue
            changed = sorted(
                set(new_snap.keys()) ^ set(snap.keys())
                | {k for k in (set(new_snap.keys()) & set(snap.keys()))
                   if new_snap[k] != snap[k]}
            )
            rel = changed[0]
            try:
                rel = str(Path(rel).resolve().relative_to(watch_root))
            except Exception:
                pass
            log.info("change detected (%s); restarting backend", rel)
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            snap = new_snap
            child = subprocess.Popen(cmd, env=env)
    except KeyboardInterrupt:
        log.info("dev reload stopped")
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()
        return 0


def _install_signals(loop: asyncio.AbstractEventLoop) -> None:
    def _handler() -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if os.name == "posix":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handler)


async def _run(
    install_root: Path | None = None,
    data_dir: Path | None = None,
) -> int:
    from .server import JarvisServer

    root = install_root if install_root is not None else _default_install_root()
    cfg = Config.load_merged(root, data_dir=data_dir)
    cfg.load_state()
    server = JarvisServer(cfg)
    _install_signals(asyncio.get_running_loop())
    try:
        await server.run()
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("jarvis")
    p.add_argument(
        "--install-root", type=Path, default=None,
        help="Application directory (config.default.yaml, backend). "
             "Default: directory above backend/jarvis.",
    )
    p.add_argument(
        "--data-dir", type=Path, default=None,
        help="User data: state, config overrides, memory, skills. "
             "Default: LOCALAPPDATA/Jarvis under Program Files, else install tree; "
             "or JARVIS_USER_DATA.",
    )
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument(
        "--dev-reload",
        action="store_true",
        help="Developer mode: auto-restart backend when repo files change.",
    )
    p.add_argument(
        "--reload-interval",
        type=float,
        default=0.8,
        help="Polling interval in seconds for --dev-reload (default: 0.8).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    from . import bootstrap

    if args.dev_reload and os.environ.get("JARVIS_RELOAD_CHILD") != "1":
        root = (
            args.install_root
            if args.install_root is not None
            else _default_install_root()
        )
        raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
        return _run_with_reloader(raw_argv, args.reload_interval, root)

    bootstrap.ensure_desktop_pip()
    bootstrap.ensure_openwakeword_models()
    try:
        return asyncio.run(_run(args.install_root, args.data_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
