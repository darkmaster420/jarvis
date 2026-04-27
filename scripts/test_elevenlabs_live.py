"""Live ElevenLabs check. Queries /voices and /user/subscription using the
key in config.yaml (or state.json / ELEVENLABS_API_KEY). Does not synth
audio; just verifies the HTTP path works."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from jarvis.config import Config   # noqa: E402
from jarvis.tts import _ElevenLabsEngine  # noqa: E402


def main() -> int:
    cfg = Config.load(ROOT / "config.yaml")
    cfg.load_state()

    eng = _ElevenLabsEngine(cfg.tts.elevenlabs)
    print("available:", eng.available)
    if not eng.available:
        print("no key set - nothing to test")
        return 0

    sub = eng.subscription()
    if sub:
        keys = ("tier", "character_count", "character_limit", "status")
        print("subscription:", {k: sub.get(k) for k in keys})

    voices = eng.list_voices()
    print(f"voices: {len(voices)} found")
    for v in voices[:5]:
        print(f"  - {v['name']} ({v['id']}) [{v.get('category','')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
