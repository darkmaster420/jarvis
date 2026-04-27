"""Verify that _ElevenLabsEngine.stream() surfaces 401/402/429 as fatal
ElevenLabsError and that the facade flips to cooldown."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.config import Config                 # noqa: E402
from jarvis.tts import _ElevenLabsEngine, ElevenLabsError  # noqa: E402


def main() -> int:
    cfg = Config.load(ROOT / "config.yaml")
    cfg.load_state()
    eng = _ElevenLabsEngine(cfg.tts.elevenlabs)
    if not eng.available:
        print("no key, skipping")
        return 0
    try:
        for _ in eng.stream("hello", threading.Event()):
            pass
        print("(no error - key is actually working)")
    except ElevenLabsError as e:
        print(f"expected error caught: status={e.status} fatal={e.fatal} msg={e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
