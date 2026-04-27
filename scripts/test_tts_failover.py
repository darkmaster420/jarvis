"""Smoke test for the ElevenLabs/Piper failover logic.

Exercises the provider-selection and cooldown code paths without
hitting the network or playing audio. Run from the repo root:

    backend\.venv\Scripts\python.exe scripts\test_tts_failover.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Clear any ambient key so the test is deterministic.
os.environ.pop("ELEVENLABS_API_KEY", None)

from jarvis.config import Config           # noqa: E402
from jarvis.tts import TextToSpeech, ElevenLabsError  # noqa: E402


def main() -> int:
    cfg = Config.load(ROOT / "config.yaml")
    # Isolate the test from anything the user already put in config.yaml.
    cfg.tts.elevenlabs.api_key = ""
    cfg.tts.elevenlabs.voice_id = "dummy-voice-id"

    # Don't actually try to play audio; stub out the worker to just log.
    tts = TextToSpeech.__new__(TextToSpeech)
    tts.cfg = cfg.tts
    tts._models_dir = cfg.models_dir()
    tts._output_device = None
    # Use the ElevenLabs engine directly but skip constructing Piper
    # (no ONNX file needed for this logic check).
    from jarvis.tts import _ElevenLabsEngine
    tts._eleven = _ElevenLabsEngine(cfg.tts.elevenlabs)
    tts._piper  = None  # type: ignore[assignment]
    import threading
    tts._cancel = threading.Event()
    tts._lock   = threading.Lock()
    tts._thread = None
    tts._on_end = None
    tts._eleven_cooldown_until = 0.0

    # 1. No key -> active provider is piper regardless of setting.
    assert not tts.elevenlabs_available
    tts.set_provider("auto")
    assert tts.active_provider == "piper", tts.active_provider
    print("[ok] no key -> piper")

    # 2. With a key -> active provider is elevenlabs.
    tts.set_elevenlabs_key("sk_fake_key_for_testing")
    assert tts.elevenlabs_available
    assert tts.active_provider == "elevenlabs", tts.active_provider
    print("[ok] key set -> elevenlabs")

    # 3. Force piper -> stays piper.
    tts.set_provider("piper")
    assert tts.active_provider == "piper"
    print("[ok] forced piper -> piper")

    # 4. Simulate a fatal 401 -> cooldown engages -> falls back in auto.
    tts.set_provider("auto")
    assert tts.active_provider == "elevenlabs"
    tts._set_cooldown("simulated 401")
    assert not tts.elevenlabs_available
    assert tts.active_provider == "piper"
    print("[ok] fatal error -> cooldown -> piper")

    # 5. Cooldown expires -> elevenlabs resumes.
    tts._eleven_cooldown_until = time.time() - 1
    assert tts.elevenlabs_available
    assert tts.active_provider == "elevenlabs"
    print("[ok] cooldown expired -> elevenlabs resumes")

    # 6. Changing provider clears cooldown.
    tts._set_cooldown("another failure")
    assert tts.active_provider == "piper"
    tts.set_provider("elevenlabs")
    assert tts.elevenlabs_available
    assert tts.active_provider == "elevenlabs"
    print("[ok] set_provider resets cooldown")

    print("\nAll TTS failover checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
