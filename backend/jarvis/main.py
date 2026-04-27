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
import signal
from .config import Config, _default_install_root


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
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    from . import bootstrap

    bootstrap.ensure_desktop_pip()
    bootstrap.ensure_openwakeword_models()
    try:
        return asyncio.run(_run(args.install_root, args.data_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
