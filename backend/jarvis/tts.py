"""Text-to-speech facade with ElevenLabs primary / Piper fallback.

Playback strategy:
  * ``provider = "elevenlabs"`` -> always try the cloud; if it fails the
    whole utterance fails (no audio).
  * ``provider = "piper"``      -> always use the local model.
  * ``provider = "auto"``       -> try ElevenLabs first; on any
    authentication/quota/rate-limit/network error, fall back to Piper and
    lock out ElevenLabs for ``fallback_cooldown_s`` seconds so we don't
    hammer a dead endpoint for every utterance.

Both engines stream raw int16 PCM into a single sounddevice OutputStream;
this keeps the barge-in/cancel semantics identical across providers.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import sounddevice as sd

from .config import ElevenLabsCfg, TtsCfg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# ElevenLabs HTTP engine
# --------------------------------------------------------------------------

class ElevenLabsError(RuntimeError):
    """Base for ElevenLabs failures. ``fatal=True`` means we should stop
    trying ElevenLabs for the rest of the cooldown (bad key / no credits)."""
    def __init__(self, message: str, *, fatal: bool = False,
                 status: int | None = None):
        super().__init__(message)
        self.fatal  = fatal
        self.status = status


class _ElevenLabsEngine:
    """Streaming PCM client for ElevenLabs TTS. No SDK dependency."""

    BASE = "https://api.elevenlabs.io/v1"

    def __init__(self, cfg: ElevenLabsCfg):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @property
    def effective_api_key(self) -> str:
        return (self.cfg.api_key or os.environ.get("ELEVENLABS_API_KEY", "") or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.effective_api_key) and bool(self.cfg.voice_id)

    def _parse_output_format(self) -> tuple[int, int, int]:
        """Returns (sample_rate, channels, bytes_per_sample) for the
        configured PCM output format. Only pcm_* formats are supported
        because we want to stream directly into sounddevice; mp3_* would
        need a decoder."""
        fmt = (self.cfg.output_format or "pcm_22050").lower()
        if not fmt.startswith("pcm_"):
            raise ElevenLabsError(
                f"Unsupported output_format={fmt!r}; use a pcm_* format.",
                fatal=True)
        try:
            sr = int(fmt.split("_", 1)[1])
        except ValueError:
            raise ElevenLabsError(f"Bad output_format: {fmt!r}", fatal=True)
        return sr, 1, 2  # mono, int16

    # ------------------------------------------------------------------
    # voice listing / quota check
    # ------------------------------------------------------------------
    def list_voices(self) -> list[dict]:
        """Returns [{id, name, category}] or []."""
        key = self.effective_api_key
        if not key:
            return []
        req = urllib.request.Request(
            self.BASE + "/voices",
            headers={"xi-api-key": key, "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning("elevenlabs list voices failed: %s", e)
            return []
        except Exception as e:
            log.warning("elevenlabs list voices failed: %s", e)
            return []
        out: list[dict] = []
        for v in data.get("voices", []):
            out.append({
                "id":       v.get("voice_id"),
                "name":     v.get("name", ""),
                "category": v.get("category", ""),
            })
        return out

    def subscription(self) -> dict | None:
        key = self.effective_api_key
        if not key:
            return None
        req = urllib.request.Request(
            self.BASE + "/user/subscription",
            headers={"xi-api-key": key, "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log.info("elevenlabs subscription check failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # streaming synthesis
    # ------------------------------------------------------------------
    def stream(self, text: str, cancel: threading.Event) -> Iterable[np.ndarray]:
        """Yields int16 ndarray chunks. Raises ElevenLabsError on failure."""
        key = self.effective_api_key
        if not key:
            raise ElevenLabsError("No API key configured.", fatal=True)

        sr, channels, _ = self._parse_output_format()
        url = (f"{self.BASE}/text-to-speech/"
               f"{urllib.parse.quote(self.cfg.voice_id)}/stream"
               f"?output_format={urllib.parse.quote(self.cfg.output_format)}")
        body = json.dumps({
            "text":     text,
            "model_id": self.cfg.model_id,
            "voice_settings": {
                "stability":        0.5,
                "similarity_boost": 0.75,
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key":   key,
                "accept":       "audio/pcm",
                "content-type": "application/json",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            # 401 bad key, 402 quota exceeded, 403 forbidden, 429 rate limit.
            # 422 is client-side (bad voice id / text); not fatal.
            status = e.code
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:400]
            except Exception:
                pass
            fatal = status in (401, 402, 403, 429)
            raise ElevenLabsError(
                f"HTTP {status}: {detail or e.reason}",
                fatal=fatal, status=status)
        except urllib.error.URLError as e:
            raise ElevenLabsError(f"network error: {e}", fatal=False)

        # The response is 16-bit little-endian PCM, mono. Yield in chunks
        # that match sounddevice's preferred buffer size.
        buf = b""
        CHUNK = 4096
        try:
            while not cancel.is_set():
                part = resp.read(CHUNK)
                if not part:
                    break
                buf += part
                # emit whole samples only
                take = (len(buf) // 2) * 2
                if take:
                    arr = np.frombuffer(buf[:take], dtype="<i2").astype(np.int16)
                    buf = buf[take:]
                    yield arr
        finally:
            try:
                resp.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Piper engine (local, offline)
# --------------------------------------------------------------------------

class _PiperEngine:
    def __init__(self, cfg: TtsCfg, models_dir: Path):
        self.cfg = cfg
        self._models_dir = models_dir
        try:
            from piper import PiperVoice
            from piper.config import SynthesisConfig
        except ImportError as e:
            raise RuntimeError(
                "piper-tts not installed. Run `pip install piper-tts`."
            ) from e
        self._PiperVoice     = PiperVoice
        self._syn_config_cls = SynthesisConfig
        self._voice = None
        self._sample_rate = 22050
        self._load_voice(cfg.voice)

    def _load_voice(self, voice_name: str) -> None:
        voice_dir = self._models_dir / "piper"
        onnx = voice_dir / f"{voice_name}.onnx"
        cfg_path = voice_dir / f"{voice_name}.onnx.json"
        if not onnx.exists() or not cfg_path.exists():
            raise FileNotFoundError(
                f"Piper voice files missing. Expected:\n  {onnx}\n  {cfg_path}\n"
                "Run: python scripts/download_models.py")
        log.info("loading Piper voice: %s", onnx)
        self._voice = self._PiperVoice.load(str(onnx), config_path=str(cfg_path))
        self._sample_rate = self._voice.config.sample_rate
        self.cfg.voice = voice_name

    def reload_voice(self, voice_name: str) -> bool:
        voice_name = (voice_name or "").strip()
        if not voice_name or voice_name == self.cfg.voice:
            return False
        try:
            self._load_voice(voice_name)
            return True
        except Exception as e:
            log.warning("failed to load voice %s: %s", voice_name, e)
            return False

    def list_voices(self) -> list[str]:
        voice_dir = self._models_dir / "piper"
        if not voice_dir.exists():
            return []
        out: list[str] = []
        for onnx in voice_dir.glob("*.onnx"):
            if onnx.with_suffix(".onnx.json").exists():
                out.append(onnx.stem)
        return sorted(out)

    def stream(self, text: str, cancel: threading.Event) -> Iterable[tuple[int, np.ndarray]]:
        """Yields (sample_rate, int16_ndarray) pairs per chunk."""
        syn_cfg = self._syn_config_cls(
            length_scale=self.cfg.length_scale,
            speaker_id=self.cfg.speaker,
        )
        for chunk in self._voice.synthesize(text, syn_cfg):
            if cancel.is_set():
                break
            sr   = chunk.sample_rate or self._sample_rate
            data = np.asarray(chunk.audio_int16_array, dtype=np.int16)
            yield sr, data


# --------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------

class TextToSpeech:
    """Non-blocking, cancellable TTS with provider failover.
    Thread-safe; only one utterance plays at a time."""

    def __init__(self, cfg: TtsCfg, models_dir: Path, output_device=None):
        self.cfg = cfg
        self._models_dir = models_dir
        self._output_device = output_device

        self._piper  = _PiperEngine(cfg, models_dir)
        self._eleven = _ElevenLabsEngine(cfg.elevenlabs)

        self._cancel = threading.Event()
        self._lock   = threading.Lock()
        self._thread: threading.Thread | None = None
        self._on_end: Callable[[], None] | None = None

        # Transient lockout: after a fatal ElevenLabs error we stop trying
        # until this unix timestamp.
        self._eleven_cooldown_until: float = 0.0

    # ------------------------------------------------------------------
    # public API expected by the rest of the app
    # ------------------------------------------------------------------
    def reload_voice(self, voice_name: str) -> bool:
        """Change the Piper voice. (ElevenLabs voice uses set_elevenlabs_voice.)"""
        self.stop()
        with self._lock:
            return self._piper.reload_voice(voice_name)

    def list_voices(self) -> list[str]:
        return self._piper.list_voices()

    def list_elevenlabs_voices(self) -> list[dict]:
        return self._eleven.list_voices()

    def set_provider(self, provider: str) -> None:
        prov = (provider or "").strip().lower()
        if prov not in ("auto", "elevenlabs", "piper"):
            raise ValueError(f"unknown provider: {provider}")
        self.cfg.provider = prov
        # Give the cloud another chance when the user explicitly asks for it.
        self._eleven_cooldown_until = 0.0

    def set_elevenlabs_key(self, key: str) -> None:
        self.cfg.elevenlabs.api_key = (key or "").strip()
        self._eleven_cooldown_until = 0.0

    def set_elevenlabs_voice(self, voice_id: str, voice_name: str = "") -> None:
        self.cfg.elevenlabs.voice_id = (voice_id or "").strip()
        if voice_name:
            self.cfg.elevenlabs.voice_name = voice_name

    def set_elevenlabs_model(self, model_id: str) -> None:
        self.cfg.elevenlabs.model_id = (model_id or "").strip()

    def is_speaking(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def stop(self) -> None:
        self._cancel.set()
        try:
            sd.stop()
        except Exception:
            pass

    def speak(self, text: str, on_end: Callable[[], None] | None = None) -> None:
        if not text.strip():
            if on_end:
                on_end()
            return
        self.stop()
        with self._lock:
            self._cancel.clear()
            self._on_end = on_end
            self._thread = threading.Thread(
                target=self._run, args=(text,),
                daemon=True, name="jarvis-tts",
            )
            self._thread.start()

    # ------------------------------------------------------------------
    # provider selection
    # ------------------------------------------------------------------
    @property
    def elevenlabs_available(self) -> bool:
        if not self._eleven.available:
            return False
        return time.time() >= self._eleven_cooldown_until

    @property
    def active_provider(self) -> str:
        """What we *would* use right now for a fresh utterance."""
        prov = (self.cfg.provider or "auto").lower()
        if prov == "piper":
            return "piper"
        if prov == "elevenlabs":
            return "elevenlabs"
        return "elevenlabs" if self.elevenlabs_available else "piper"

    def _set_cooldown(self, reason: str) -> None:
        secs = max(1, int(self.cfg.elevenlabs.fallback_cooldown_s))
        self._eleven_cooldown_until = time.time() + secs
        log.warning("ElevenLabs disabled for %ds: %s", secs, reason)

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _run(self, text: str) -> None:
        try:
            self._run_with_failover(text)
        except Exception as e:
            log.exception("TTS pipeline error: %s", e)
        finally:
            cb = self._on_end
            self._on_end = None
            if cb:
                try:
                    cb()
                except Exception:  # pragma: no cover
                    log.exception("tts on_end callback failed")

    def _run_with_failover(self, text: str) -> None:
        prov = (self.cfg.provider or "auto").lower()
        try_eleven = prov in ("auto", "elevenlabs") and self.elevenlabs_available

        if try_eleven:
            try:
                self._play_eleven(text)
                return
            except ElevenLabsError as e:
                if e.fatal:
                    self._set_cooldown(str(e))
                log.info("ElevenLabs failed (%s); falling back to Piper", e)
                if prov == "elevenlabs":
                    # User explicitly chose cloud-only - don't secretly fall back.
                    return
            except Exception as e:
                log.warning("ElevenLabs crashed: %s; falling back to Piper", e)
                if prov == "elevenlabs":
                    return

        if prov == "elevenlabs" and not self.elevenlabs_available:
            log.warning("ElevenLabs forced but unavailable; nothing to speak")
            return
        self._play_piper(text)

    def _play_eleven(self, text: str) -> None:
        sr, channels, _bps = self._eleven._parse_output_format()
        stream: sd.OutputStream | None = None
        try:
            for arr in self._eleven.stream(text, self._cancel):
                if self._cancel.is_set():
                    break
                if stream is None:
                    stream = sd.OutputStream(
                        samplerate=sr, channels=channels,
                        dtype="int16", device=self._output_device,
                    )
                    stream.start()
                stream.write(arr)
        finally:
            if stream is not None:
                stream.stop(); stream.close()

    def _play_piper(self, text: str) -> None:
        stream: sd.OutputStream | None = None
        sr = self._piper._sample_rate
        try:
            for chunk_sr, data in self._piper.stream(text, self._cancel):
                if self._cancel.is_set():
                    break
                if stream is None:
                    sr = chunk_sr
                    stream = sd.OutputStream(
                        samplerate=sr, channels=1, dtype="int16",
                        device=self._output_device,
                    )
                    stream.start()
                stream.write(data)
        finally:
            if stream is not None:
                stream.stop(); stream.close()
