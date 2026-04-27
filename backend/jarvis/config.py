"""Configuration loader for Jarvis."""
from __future__ import annotations

import copy
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: ``over`` wins; nested dicts merge one level at a time."""
    out: dict[str, Any] = copy.deepcopy(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)  # type: ignore[assignment]
        else:
            out[k] = v
    return out


def _default_install_root() -> Path:
    """``backend/jarvis/config.py`` → parent³ = app / repo root (with ``backend/`` and ``Jarvis.exe``)."""
    return Path(__file__).resolve().parent.parent.parent


def user_data_dir(install_root: Path) -> Path:
    """User-editable data (config overrides, state, memory, skills, layout).

    * ``JARVIS_USER_DATA`` — explicit directory (highest priority).
    * Installs under *Program Files* (or *Program Files (x86)*) — ``%LocalAppData%\\Jarvis``.
    * Dev trees outside Program Files — same directory as the install (state/profiles
      next to the repo) so a single tree ``config.yaml`` keeps working.
    """
    ovr = (os.environ.get("JARVIS_USER_DATA") or "").strip()
    if ovr:
        return Path(ovr)
    ins = str(install_root.resolve()).lower()
    if ":\\program files" in ins or "program files (x86)" in ins:
        la = os.environ.get("LOCALAPPDATA", "")
        if la:
            p = Path(la) / "Jarvis"
            p.mkdir(parents=True, exist_ok=True)
            return p
    # Dev / non–Program-Files: keep data next to the install (writable).
    return install_root


def _maybe_migrate_legacy_data(install_root: Path, data_dir: Path) -> None:
    """One-time copy from old in-install paths (e.g. before AppData) into ``data_dir``."""
    if data_dir == install_root:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "hud_layout.json", "config.yaml"):
        src = install_root / name
        dst = data_dir / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    for dname in ("memory", "user_skills", "profiles", "proposed_patches"):
        src = install_root / dname
        dst = data_dir / dname
        if src.is_dir() and any(src.iterdir()) and not dst.exists():
            try:
                shutil.copytree(src, dst)
            except OSError:
                pass


@dataclass
class AudioCfg:
    sample_rate: int = 16000
    frame_ms: int = 20
    input_device: str | int | None = None
    output_device: str | int | None = None


@dataclass
class WakeWordCfg:
    # One or more wake words. Each entry is either a built-in openWakeWord
    # model name (e.g. "hey_jarvis", "alexa", "hey_mycroft") or the absolute
    # path to a custom *.onnx file.
    models: list[str] = field(default_factory=lambda: ["hey_jarvis"])
    # Global activation threshold. Used unless overridden per-model below.
    threshold: float = 0.5
    # Optional per-model overrides, keyed by the model name reported by
    # openWakeWord (usually the filename stem).
    thresholds: dict[str, float] = field(default_factory=dict)
    # Minimum gap between triggers to avoid double-firing across models.
    cooldown_s: float = 1.5


@dataclass
class VadCfg:
    threshold: float = 0.5
    min_silence_ms: int = 700
    max_utterance_s: int = 15
    pre_roll_ms: int = 300


@dataclass
class SttCfg:
    model: str = "base.en"
    device: str = "auto"
    compute_type: str = "int8"
    beam_size: int = 1


@dataclass
class SpeakerIdCfg:
    enabled: bool = True
    threshold: float = 0.75
    profiles_dir: str = "profiles"
    # Utterances collected per completion (enrollment or "add more" refine).
    enroll_samples: int = 8
    # Minimum seconds of audio per phrase (skip if shorter); ~1.0–1.5 works well.
    enroll_min_utterance_s: float = 1.4


@dataclass
class ElevenLabsCfg:
    api_key: str = ""  # empty => check ELEVENLABS_API_KEY env var
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" (default preset voice)
    voice_name: str = "Rachel"
    model_id: str = "eleven_turbo_v2_5"
    # PCM @ 22_050 Hz is the cheapest format on the free tier and works
    # directly with sounddevice without decoding.
    output_format: str = "pcm_22050"
    # How long to lock out ElevenLabs after a 401/402/429 before retrying.
    fallback_cooldown_s: int = 600


@dataclass
class TtsCfg:
    # "auto" = try ElevenLabs first then Piper; "elevenlabs" = force cloud;
    # "piper" = force local.
    provider: str = "auto"
    voice: str = "en_US-lessac-medium"
    speaker: int = 0
    length_scale: float = 1.0
    elevenlabs: ElevenLabsCfg = field(default_factory=ElevenLabsCfg)


@dataclass
class LlmCfg:
    provider: str = "ollama"
    host: str = "http://127.0.0.1:11434"
    # If the API is down and `host` is loopback, try to start the Ollama app
    # (Windows, minimized) so the server comes up in the tray.
    auto_start_ollama: bool = True
    ollama_app_path: str | None = None  # optional; default search under LocalAppData/ProgramFiles
    model: str = "qwen2.5vl:7b"
    system_prompt: str = "You are Jarvis, a helpful assistant."
    # Use one multimodal model for both text/tools and vision by default; this
    # avoids slow model swaps. For higher quality, set both `model` and
    # `vision_model` to qwen2.5vl:32b if the machine can run it.
    vision_model: str = "qwen2.5vl:7b"
    vision_fallback_models: list[str] = field(default_factory=lambda: ["gemma3:4b"])
    # Wider = easier for the VLM to read UI text (slightly slower to encode).
    vision_max_screenshot_w: int = 1024
    # One round = one screenshot + one model decision (usually one action).
    vision_desktop_rounds: int = 8
    # Let Windows repaint before the next capture (seconds).
    vision_post_action_s: float = 0.18
    # Cap vision JSON length so the model returns faster (Ollama option).
    vision_num_predict: int = 384


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class PermissionsCfg:
    owner: str = "owner"
    restricted_intents: list[str] = field(default_factory=list)


@dataclass
class Config:
    # ``root`` = install directory (read-only in Program Files; contains backend/, models/, …).
    # ``data_dir`` = per-user data (e.g. %LocalAppData%\Jarvis) — state, memory, config overrides.
    audio: AudioCfg = field(default_factory=AudioCfg)
    wake_word: WakeWordCfg = field(default_factory=WakeWordCfg)
    vad: VadCfg = field(default_factory=VadCfg)
    stt: SttCfg = field(default_factory=SttCfg)
    speaker_id: SpeakerIdCfg = field(default_factory=SpeakerIdCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    server: ServerCfg = field(default_factory=ServerCfg)
    permissions: PermissionsCfg = field(default_factory=PermissionsCfg)
    root: Path = field(default_factory=lambda: Path.cwd())
    data_dir: Path = field(default_factory=lambda: Path.cwd())

    @classmethod
    def _from_data(
        cls, data: dict[str, Any], install_root: Path, data_dir: Path,
    ) -> "Config":
        tts_raw = dict(data.get("tts", {}))
        eleven_raw = tts_raw.pop("elevenlabs", {}) or {}
        tts_cfg = TtsCfg(**tts_raw, elevenlabs=ElevenLabsCfg(**eleven_raw))
        llm_raw = dict(data.get("llm", {}))
        if str(llm_raw.get("vision_model", "")).startswith("qwen2.5-vl:"):
            llm_raw["vision_model"] = str(llm_raw["vision_model"]).replace(
                "qwen2.5-vl:", "qwen2.5vl:", 1)

        wake_raw = dict(data.get("wake_word", {}))
        if "model" in wake_raw and "models" not in wake_raw:
            m = wake_raw.pop("model")
            wake_raw["models"] = [m] if isinstance(m, str) else list(m or [])
        elif "model" in wake_raw:
            wake_raw.pop("model", None)

        return cls(
            audio=AudioCfg(**data.get("audio", {})),
            wake_word=WakeWordCfg(**wake_raw),
            vad=VadCfg(**data.get("vad", {})),
            stt=SttCfg(**data.get("stt", {})),
            speaker_id=SpeakerIdCfg(**data.get("speaker_id", {})),
            tts=tts_cfg,
            llm=LlmCfg(**llm_raw),
            server=ServerCfg(**data.get("server", {})),
            permissions=PermissionsCfg(**data.get("permissions", {})),
            root=install_root,
            data_dir=data_dir,
        )

    @classmethod
    def load_merged(
        cls,
        install_root: str | Path,
        data_dir: str | Path | None = None,
    ) -> "Config":
        """Load ``config.default.yaml`` (or ``config.yaml``) from *install_root*,
        merge *optional* ``<data_dir>/config.yaml`` on top, and attach paths.
        New keys in the default file are picked up; user overrides win on conflicts.
        """
        install_root = Path(install_root)
        data_dir = Path(data_dir) if data_dir is not None else user_data_dir(install_root)
        _maybe_migrate_legacy_data(install_root, data_dir)
        def_path = install_root / "config.default.yaml"
        leg_path = install_root / "config.yaml"
        if def_path.is_file():
            defaults_path = def_path
        elif leg_path.is_file():
            defaults_path = leg_path
        else:
            raise FileNotFoundError(
                f"Neither config.default.yaml nor config.yaml found in {install_root}"
            )
        user_path = data_dir / "config.yaml"
        d_def: dict[str, Any] = yaml.safe_load(
            defaults_path.read_text(encoding="utf-8")) or {}
        if user_path.is_file() and user_path.resolve() != defaults_path.resolve():
            d_user: dict[str, Any] = yaml.safe_load(
                user_path.read_text(encoding="utf-8")) or {}
            data = _deep_merge(d_def, d_user)
        else:
            data = d_def
        return cls._from_data(data, install_root, data_dir)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Back-compat: *path* to a ``config.yaml`` file, or a directory; uses that tree for
        both install and data (typical in tests / scripts).
        """
        p = Path(path)
        if p.is_file():
            inst = p.parent
        elif p.is_dir():
            inst = p
        elif p.suffix.lower() in (".yaml", ".yml"):
            inst = p.parent
        else:
            raise FileNotFoundError(f"Not a file or directory: {path}")
        return cls.load_merged(inst, data_dir=inst)

    def models_dir(self) -> Path:
        return self.root / "models"

    def profiles_dir(self) -> Path:
        rel = Path(self.speaker_id.profiles_dir)
        if rel.is_absolute():
            return rel
        return (self.data_dir / rel).resolve()

    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    def load_state(self) -> None:
        """Apply any persisted runtime overrides from state.json on top of
        the values loaded from config.yaml. Runs at startup."""
        import json
        p = self.state_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if voice := data.get("voice"):
            self.tts.voice = str(voice)
        if model := data.get("llm_model"):
            self.llm.model = str(model)
        if prov := data.get("tts_provider"):
            self.tts.provider = str(prov)
        if "speaker_enabled" in data:
            self.speaker_id.enabled = bool(data["speaker_enabled"])
        if (thr := data.get("speaker_threshold")) is not None:
            try:
                self.speaker_id.threshold = float(thr)
            except (TypeError, ValueError):
                pass
        if owner := data.get("owner"):
            self.permissions.owner = str(owner)
        eleven = data.get("elevenlabs") or {}
        if isinstance(eleven, dict):
            if "api_key" in eleven:
                self.tts.elevenlabs.api_key = str(eleven.get("api_key") or "")
            if (v := eleven.get("voice_id")):   self.tts.elevenlabs.voice_id  = str(v)
            if (n := eleven.get("voice_name")): self.tts.elevenlabs.voice_name= str(n)
            if (m := eleven.get("model_id")):   self.tts.elevenlabs.model_id  = str(m)

    def save_state(
        self, *,
        voice: str | None = None,
        llm_model: str | None = None,
        tts_provider: str | None = None,
        speaker_enabled: bool | None = None,
        speaker_threshold: float | None = None,
        owner: str | None = None,
        elevenlabs_api_key: str | None = None,
        elevenlabs_voice_id: str | None = None,
        elevenlabs_voice_name: str | None = None,
        elevenlabs_model_id: str | None = None,
    ) -> None:
        """Write the currently-selected runtime overrides to state.json.
        Merges with any previously saved values."""
        import json
        p = self.state_path()
        data: dict[str, Any] = {}
        if p.exists():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass
        if voice is not None:             data["voice"]             = voice
        if llm_model is not None:         data["llm_model"]         = llm_model
        if tts_provider is not None:      data["tts_provider"]      = tts_provider
        if speaker_enabled is not None:   data["speaker_enabled"]   = bool(speaker_enabled)
        if speaker_threshold is not None: data["speaker_threshold"] = float(speaker_threshold)
        if owner is not None:             data["owner"]             = owner
        eleven = dict(data.get("elevenlabs") or {})
        if elevenlabs_api_key    is not None: eleven["api_key"]    = elevenlabs_api_key
        if elevenlabs_voice_id   is not None: eleven["voice_id"]   = elevenlabs_voice_id
        if elevenlabs_voice_name is not None: eleven["voice_name"] = elevenlabs_voice_name
        if elevenlabs_model_id   is not None: eleven["model_id"]   = elevenlabs_model_id
        if eleven:
            data["elevenlabs"] = eleven
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
