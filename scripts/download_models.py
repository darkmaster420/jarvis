"""Download all models used by Jarvis into ./models.

Idempotent: skips files that already exist. Requires internet.

Usage:
    python scripts/download_models.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.request import urlretrieve

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# voice_name -> relative path under piper-voices
PIPER_VOICES = {
    "en_US-lessac-medium":       "en/en_US/lessac/medium",
    "en_US-ryan-medium":         "en/en_US/ryan/medium",
    "en_US-amy-medium":          "en/en_US/amy/medium",
    "en_US-hfc_female-medium":   "en/en_US/hfc_female/medium",
    "en_US-hfc_male-medium":     "en/en_US/hfc_male/medium",
    "en_GB-alan-medium":         "en/en_GB/alan/medium",
    "en_GB-jenny_dioco-medium":  "en/en_GB/jenny_dioco/medium",
    "en_GB-northern_english_male-medium": "en/en_GB/northern_english_male/medium",
}

DEFAULT_VOICES = ["en_US-lessac-medium"]


def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} (already present)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  [get]  {url}\n         -> {dest}")

    def _hook(block: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = min(block * block_size, total)
        pct = 100.0 * done / total
        sys.stdout.write(f"\r         {pct:5.1f}%  ({done/1e6:.1f}/{total/1e6:.1f} MB)")
        sys.stdout.flush()

    urlretrieve(url, dest, _hook)
    sys.stdout.write("\n")


def download_piper(voices: list[str] | None = None) -> None:
    voices = voices or DEFAULT_VOICES
    voices_dir = MODELS / "piper"
    for voice in voices:
        rel = PIPER_VOICES.get(voice)
        if rel is None:
            print(f"  [skip] unknown voice {voice!r}. "
                  f"Available: {', '.join(sorted(PIPER_VOICES))}")
            continue
        print(f"Piper voice '{voice}' ...")
        base = f"{PIPER_BASE}/{rel}"
        _download(f"{base}/{voice}.onnx",      voices_dir / f"{voice}.onnx")
        _download(f"{base}/{voice}.onnx.json", voices_dir / f"{voice}.onnx.json")


def download_openwakeword() -> None:
    """openWakeWord downloads its own ONNX files on first instantiation to
    ~/.openwakeword. We pre-warm by importing it once here."""
    print("openWakeWord (pre-warm) ...")
    try:
        import openwakeword  # noqa: F401
        from openwakeword.utils import download_models as owd

        owd()
        print("  [ok] openWakeWord models present")
    except Exception as e:  # pragma: no cover - best effort
        print(f"  [warn] could not pre-warm openWakeWord: {e}")


def download_whisper() -> None:
    """faster-whisper pulls CTranslate2 weights from HF on first use.
    Pre-warm by instantiating the default model."""
    print("faster-whisper base.en (pre-warm) ...")
    try:
        from faster_whisper import WhisperModel

        WhisperModel("base.en", device="cpu", compute_type="int8")
        print("  [ok] whisper model cached")
    except Exception as e:  # pragma: no cover
        print(f"  [warn] could not pre-warm whisper: {e}")


def download_resemblyzer() -> None:
    """Resemblyzer bundles its encoder weights in the package."""
    print("Resemblyzer (verify) ...")
    try:
        from resemblyzer import VoiceEncoder

        VoiceEncoder("cpu")
        print("  [ok] resemblyzer encoder ready")
    except Exception as e:  # pragma: no cover
        print(f"  [warn] resemblyzer: {e}")


def download_silero_vad() -> None:
    print("silero-vad (pre-warm) ...")
    try:
        from silero_vad import load_silero_vad

        load_silero_vad()
        print("  [ok] silero-vad ready")
    except Exception as e:  # pragma: no cover
        print(f"  [warn] silero-vad: {e}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voices", nargs="*", default=None,
        help=("Piper voices to download. Default: just en_US-lessac-medium. "
              "Pass 'all' to grab every voice, or one or more names. "
              f"Known: {', '.join(sorted(PIPER_VOICES))}"),
    )
    parser.add_argument(
        "--voices-only", action="store_true",
        help="Only download Piper voices, skip other models.",
    )
    args = parser.parse_args(argv)

    MODELS.mkdir(parents=True, exist_ok=True)

    voices = args.voices
    if voices and voices == ["all"]:
        voices = list(PIPER_VOICES)

    download_piper(voices)
    if args.voices_only:
        print("\nVoices ready in:", MODELS / "piper")
        return 0

    download_openwakeword()
    download_silero_vad()
    download_whisper()
    download_resemblyzer()
    print("\nAll models ready in:", MODELS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
