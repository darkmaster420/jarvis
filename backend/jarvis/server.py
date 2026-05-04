"""Jarvis core server.

Owns the assistant state machine and runs a WebSocket endpoint the HUD
clients connect to. States: idle -> listening -> thinking -> speaking.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import time
from typing import Any

import numpy as np
import websockets
from websockets.server import WebSocketServerProtocol

from .audio import AudioCapture
from .config import Config
from .memory import Memory
from .orchestrator import Orchestrator
from .patches import PatchManager
from .speaker_id import SpeakerID
from .stt import SpeechToText
from .tts import TextToSpeech
from .user_skills import UserSkillManager
from .vad import VoiceActivityDetector
from .wakeword import WakeWord
from .bootstrap import start_ollama_bootstrap_thread
from .skills import desktop
from .skills.system import prewarm_start_menu_cache

log = logging.getLogger(__name__)


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_FENCE_RE = re.compile(r"```(?:\w+)?\s*([\s\S]*?)```")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_EMPH_RE = re.compile(r"(?<!\w)(\*\*|__|\*|_|~~)(.+?)\1(?!\w)")
_LINE_PREFIX_RE = re.compile(r"(?m)^\s{0,3}(?:[-*+]|\d+\.)\s+")
_CAMERA_PROMPT_RE = re.compile(r"\b(camera|webcam|photo|snapshot|picture)\b", re.I)
_SCREEN_PROMPT_RE = re.compile(r"\b(screen|window|display|desktop)\b", re.I)
_COMMAND_PROMPT_RE = re.compile(
    r"\b(open|start|launch|run|close|stop|restart|set|turn|mute|unmute|lock|shutdown)\b",
    re.I,
)


def _clean_for_speech(text: str) -> str:
    """Strip common markdown/styling artifacts before TTS/HUD output."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _MD_FENCE_RE.sub(lambda m: (m.group(1) or "").strip(), t)
    t = _MD_INLINE_CODE_RE.sub(r"\1", t)
    # Run emphasis cleanup a few times to catch nested formatting.
    for _ in range(3):
        nt = _MD_EMPH_RE.sub(r"\2", t)
        if nt == t:
            break
        t = nt
    t = _LINE_PREFIX_RE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


class State(str, enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class EnrollmentSession:
    def __init__(self, name: str, *, target: int, refine: bool = False):
        self.name = name
        self.collected = 0
        self.target = max(2, target)
        self.refine = refine


class JarvisServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.loop = asyncio.get_running_loop()
        self.audio = AudioCapture(cfg.audio, loop=self.loop)
        self.wake = WakeWord(cfg.wake_word)
        self.vad = VoiceActivityDetector(cfg.vad)
        self.stt = SpeechToText(cfg.stt, cache_dir=cfg.models_dir() / "whisper")
        self.speaker = SpeakerID(cfg.speaker_id, cfg.profiles_dir())
        self.tts = TextToSpeech(cfg.tts, cfg.models_dir(),
                                 output_device=cfg.audio.output_device)
        self.memory = Memory(cfg.data_dir / "memory")
        self.user_skills = UserSkillManager(
            skills_dir=cfg.data_dir / "user_skills",
            repo_root=cfg.root,
        )
        self.patches = PatchManager(
            repo_root=cfg.root,
            patch_dir=cfg.data_dir / "proposed_patches",
            user_skills_dir=cfg.data_dir / "user_skills",
        )
        self.orch = Orchestrator(
            cfg.llm, cfg.permissions,
            memory=self.memory,
            user_skills=self.user_skills,
            patches=self.patches,
        )
        # Register existing user skills so they show up as LLM tools.
        self.user_skills.bind(
            self.orch.register_user_skill,
            self.orch.unregister_user_skill,
        )
        self.user_skills.load_all()

        self.state = State.IDLE
        self.muted = False
        self.clients: set[WebSocketServerProtocol] = set()
        self._pending_utterance: list[np.ndarray] = []
        self._utter_started: float = 0.0
        self._enroll: EnrollmentSession | None = None
        self._turn_id = 0
        self._broadcast_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._elevenlabs_voice_cache: list[dict] = []
        self._narration_lock = asyncio.Lock()
        self._silence_nudge_task: asyncio.Task | None = None
        self._turn_progress_announced: set[int] = set()
        self._turn_last_progress_at: dict[int, float] = {}
        self._turn_progress_idx: dict[int, int] = {}
        # False after background ensure_ollama_models() returns (HUD can show Ollama status).
        self._ollama_bootstrap_pending = True
        self._live_camera_task: asyncio.Task | None = None
        self._live_camera_stream: desktop.LiveCameraStream | None = None

    async def broadcast(self, event: str, **data: Any) -> None:
        payload = {"event": event, **data}
        await self._broadcast_queue.put(payload)

    async def _broadcaster(self) -> None:
        while True:
            payload = await self._broadcast_queue.get()
            if not self.clients:
                continue
            msg = json.dumps(payload)
            dead = []
            for ws in list(self.clients):
                try:
                    await ws.send(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    async def _set_state(self, s: State) -> None:
        if s == self.state:
            return
        self.state = s
        log.info("state -> %s", s.value)
        await self.broadcast("state", state=s.value)

    async def _start_live_camera(self) -> tuple[bool, str]:
        if self._live_camera_task is not None and not self._live_camera_task.done():
            return True, "Live camera view is already running."
        cam_idx = int(getattr(self.cfg.llm, "vision_camera_index", 0) or 0)
        cam_w = int(getattr(self.cfg.llm, "vision_camera_max_width", 960) or 960)
        fps = float(getattr(self.cfg.llm, "vision_live_fps", 5.0) or 5.0)
        fps = max(1.0, min(15.0, fps))
        stream = desktop.LiveCameraStream(camera_index=cam_idx, max_width=cam_w)
        ok, msg = await asyncio.to_thread(stream.open)
        if not ok:
            return False, msg
        self._live_camera_stream = stream

        async def _loop() -> None:
            interval = 1.0 / fps
            try:
                while True:
                    if self._live_camera_stream is None:
                        return
                    try:
                        b64 = await asyncio.to_thread(
                            self._live_camera_stream.read_frame_b64
                        )
                        if b64:
                            await self.broadcast(
                                "camera_frame",
                                image_b64=b64,
                                mime="image/jpeg",
                                live=True,
                            )
                    except Exception as e:
                        log.warning("live camera frame failed: %s", e)
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

        self._live_camera_task = asyncio.create_task(_loop())
        await self.broadcast("camera_live", active=True, fps=fps)
        return True, f"Live camera view started at about {int(round(fps))} FPS."

    async def _stop_live_camera(self) -> tuple[bool, str]:
        if self._live_camera_task is not None:
            self._live_camera_task.cancel()
            self._live_camera_task = None
        if self._live_camera_stream is not None:
            await asyncio.to_thread(self._live_camera_stream.close)
            self._live_camera_stream = None
        await self.broadcast("camera_live", active=False)
        return True, "Live camera view stopped."

    def _settings_snapshot(self) -> dict:
        eleven = self.cfg.tts.elevenlabs
        key = (self.tts._eleven.effective_api_key or "").strip()
        # Never echo the full API key to clients; the HUD only needs to know
        # that a key is set and a short prefix/suffix to refill the textbox.
        key_hint = ""
        if len(key) >= 8:
            key_hint = f"{key[:4]}...{key[-4:]}"
        return {
            "current": {
                "llm_model":             self.cfg.llm.model,
                "voice":                 self.cfg.tts.voice,
                "tts_provider":          self.cfg.tts.provider,
                "tts_active_provider":   self.tts.active_provider,
                "elevenlabs_voice_id":   eleven.voice_id,
                "elevenlabs_voice_name": eleven.voice_name,
                "elevenlabs_model_id":   eleven.model_id,
                "elevenlabs_has_key":    bool(self.tts._eleven.effective_api_key),
                "elevenlabs_api_key_hint": key_hint,
                "speaker_enabled":       bool(self.cfg.speaker_id.enabled),
                "speaker_threshold":     float(self.cfg.speaker_id.threshold),
                "owner":                 self.cfg.permissions.owner,
                "enroll_samples":        int(self.speaker.enroll_target()),
            },
            "available": {
                "llm_models":        self.orch.list_models(),
                "voices":            self.tts.list_voices(),
                "tts_providers":     ["auto", "elevenlabs", "piper"],
                "elevenlabs_voices": self._elevenlabs_voice_cache,
                "profiles":          list(self.speaker.profiles()),
            },
            "ollama": {
                "ready": not self._ollama_bootstrap_pending,
            },
        }

    async def _after_ollama_bootstrap(self) -> None:
        self._ollama_bootstrap_pending = False
        await self.broadcast("settings", **self._settings_snapshot())

    async def _refresh_elevenlabs_voices(self) -> None:
        """Pull the cloud voice list in a background thread and stash it so
        subsequent settings snapshots include it."""
        if not self.tts._eleven.effective_api_key:
            self._elevenlabs_voice_cache = []
            return
        self._elevenlabs_voice_cache = await asyncio.to_thread(
            self.tts.list_elevenlabs_voices)

    async def handle_client(self, ws: WebSocketServerProtocol) -> None:
        self.clients.add(ws)
        log.info("client connected (%d total)", len(self.clients))
        try:
            await ws.send(json.dumps({
                "event": "hello",
                "state": self.state.value,
                "muted": self.muted,
                "profiles": list(self.speaker.profiles()),
                "settings": self._settings_snapshot(),
            }))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._on_client_message(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log.info("client disconnected (%d remain)", len(self.clients))

    async def _on_client_message(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "push_to_talk":
            if self.state == State.IDLE:
                await self._begin_listening()
        elif cmd == "cancel":
            await self._interrupt_current("cancelled")
        elif cmd == "mute":
            self.muted = bool(msg.get("value", not self.muted))
            await self.broadcast("muted", value=self.muted)
        elif cmd == "enroll_start":
            name = (msg.get("name") or "").strip().lower()
            refine = bool(msg.get("refine", False))
            if name:
                if refine and not self.speaker.has_profile(name):
                    await self.broadcast(
                        "error",
                        message=(
                            f"No saved profile named '{name}'. Enroll first, "
                            "then use add more samples to improve it."
                        ),
                    )
                else:
                    tgt = self.speaker.enroll_target()
                    self._enroll = EnrollmentSession(
                        name, target=tgt, refine=refine,
                    )
                    await self.broadcast(
                        "enroll_progress", name=name, collected=0, target=tgt,
                        refine=refine,
                    )
                    await self._begin_listening()
        elif cmd == "enroll_cancel":
            if self._enroll is not None:
                self.speaker.cancel_enroll(self._enroll.name)
            self._enroll = None
            await self.broadcast("enroll_cancelled")
        elif cmd == "say":
            text = (msg.get("text") or "").strip()
            if text:
                await self._speak(text)
        elif cmd == "prompt":
            text = (msg.get("text") or "").strip()
            if text:
                await self._process_text_prompt(text, user="guest")
        elif cmd == "list_settings":
            await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "list_user_skills":
            items = await asyncio.to_thread(self.user_skills.list)
            await self.broadcast("user_skills", items=items)
        elif cmd == "set_llm_model":
            model = (msg.get("model") or "").strip()
            if model:
                self.orch.set_model(model)
                self.cfg.llm.model = model
                await asyncio.to_thread(self.cfg.save_state, llm_model=model)
                await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_voice":
            voice = (msg.get("voice") or "").strip()
            if voice:
                ok = await asyncio.to_thread(self.tts.reload_voice, voice)
                if ok:
                    self.cfg.tts.voice = voice
                    await asyncio.to_thread(self.cfg.save_state, voice=voice)
                    await self.broadcast("settings", **self._settings_snapshot())
                    await self._speak(f"Voice switched to {voice}.")
                else:
                    await self.broadcast("error",
                                         message=f"Could not load voice {voice}")
        elif cmd == "set_tts_provider":
            prov = (msg.get("provider") or "").strip().lower()
            try:
                self.tts.set_provider(prov)
            except ValueError as e:
                await self.broadcast("error", message=str(e))
            else:
                await asyncio.to_thread(self.cfg.save_state, tts_provider=prov)
                await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_elevenlabs_key":
            key = (msg.get("key") or "").strip()
            self.tts.set_elevenlabs_key(key)
            await asyncio.to_thread(self.cfg.save_state,
                                    elevenlabs_api_key=key)
            await self._refresh_elevenlabs_voices()
            await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_elevenlabs_voice":
            vid  = (msg.get("voice_id") or "").strip()
            name = (msg.get("voice_name") or "").strip()
            if vid:
                self.tts.set_elevenlabs_voice(vid, name)
                await asyncio.to_thread(
                    self.cfg.save_state,
                    elevenlabs_voice_id=vid,
                    elevenlabs_voice_name=name or None,
                )
                await self.broadcast("settings", **self._settings_snapshot())
                await self._speak(f"ElevenLabs voice set to {name or vid}.")
        elif cmd == "set_elevenlabs_model":
            mid = (msg.get("model_id") or "").strip()
            if mid:
                self.tts.set_elevenlabs_model(mid)
                await asyncio.to_thread(self.cfg.save_state,
                                        elevenlabs_model_id=mid)
                await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "refresh_elevenlabs_voices":
            await self._refresh_elevenlabs_voices()
            await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_speaker_enabled":
            val = bool(msg.get("value", True))
            self.cfg.speaker_id.enabled = val
            await asyncio.to_thread(self.cfg.save_state, speaker_enabled=val)
            await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_speaker_threshold":
            try:
                thr = float(msg.get("value", self.cfg.speaker_id.threshold))
            except (TypeError, ValueError):
                return
            thr = max(0.0, min(1.0, thr))
            self.cfg.speaker_id.threshold = thr
            await asyncio.to_thread(self.cfg.save_state, speaker_threshold=thr)
            await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "set_owner":
            owner = (msg.get("owner") or "").strip().lower()
            if owner:
                self.cfg.permissions.owner = owner
                await asyncio.to_thread(self.cfg.save_state, owner=owner)
                await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "delete_profile":
            name = (msg.get("name") or "").strip().lower()
            if name:
                ok = await asyncio.to_thread(self.speaker.delete_profile, name)
                if ok:
                    # If the deleted profile was the owner, fall back to the
                    # first remaining one (or "owner" placeholder).
                    if self.cfg.permissions.owner.lower() == name:
                        remaining = list(self.speaker.profiles())
                        new_owner = remaining[0] if remaining else "owner"
                        self.cfg.permissions.owner = new_owner
                        await asyncio.to_thread(self.cfg.save_state,
                                                owner=new_owner)
                await self.broadcast("profiles",
                                     items=list(self.speaker.profiles()))
                await self.broadcast("settings", **self._settings_snapshot())
        elif cmd == "list_patches":
            patches = await asyncio.to_thread(self.patches.list_patches)
            await self.broadcast("patches", items=patches)
        elif cmd == "approve_patch":
            pid = (msg.get("id") or "").strip()
            if pid:
                try:
                    info = await asyncio.to_thread(self.patches.approve, pid)
                    applied = (info.get("applied") or "").replace("\\", "/")
                    if applied == "user_skills/skills.py":
                        await asyncio.to_thread(self.user_skills.reload_all)
                        speak = (
                            "Patch applied to user_skills/skills.py. "
                            "I reloaded your tools; no restart needed."
                        )
                    else:
                        speak = (
                            f"Patch applied to {info['applied']}. "
                            "Core Python changes load only after a backend restart: "
                            "type /restart in the HUD, or quit and launch Jarvis again."
                        )
                    await self.broadcast(
                        "patch_applied",
                        id=info["id"],
                        target=info["applied"],
                        abs_path=info.get("abs_path") or "",
                    )
                    patches = await asyncio.to_thread(self.patches.list_patches)
                    await self.broadcast("patches", items=patches)
                    await self._speak(speak)
                except Exception as e:
                    msg_text = str(e)
                    if "no such patch" in msg_text.lower():
                        await self.broadcast(
                            "error",
                            message=(
                                "That patch no longer exists (likely already applied or rejected). "
                                "Refreshing patch list."
                            ),
                        )
                        patches = await asyncio.to_thread(self.patches.list_patches)
                        await self.broadcast("patches", items=patches)
                    else:
                        await self.broadcast("error",
                                             message=f"Patch failed: {e}")
        elif cmd == "reject_patch":
            pid = (msg.get("id") or "").strip()
            if pid:
                await asyncio.to_thread(self.patches.reject, pid)
                patches = await asyncio.to_thread(self.patches.list_patches)
                await self.broadcast("patches", items=patches)

    async def _begin_listening(self) -> None:
        self._turn_id += 1
        self._turn_progress_announced = {
            t for t in self._turn_progress_announced if t >= self._turn_id - 4
        }
        self._turn_last_progress_at = {
            t: ts for t, ts in self._turn_last_progress_at.items()
            if t >= self._turn_id - 4
        }
        self._turn_progress_idx = {
            t: idx for t, idx in self._turn_progress_idx.items()
            if t >= self._turn_id - 4
        }
        self.tts.stop()
        pre_roll = self.audio.ring.read_last(self.cfg.vad.pre_roll_ms / 1000.0)
        self._pending_utterance = [pre_roll] if pre_roll.size else []
        self.vad.reset()
        self._utter_started = time.monotonic()
        await self._set_state(State.LISTENING)
        await self.broadcast("listening")

    async def _interrupt_current(self, reason: str = "interrupted",
                                 *, listen_after: bool = False) -> None:
        """Stop speech and invalidate any in-flight STT/LLM turn.

        Work running in a background thread cannot always be killed (for
        example an Ollama request), so we advance `_turn_id`; when that work
        returns, its result is ignored instead of being spoken.
        """
        self._turn_id += 1
        if self._silence_nudge_task is not None:
            self._silence_nudge_task.cancel()
            self._silence_nudge_task = None
        self.orch.cancel_inflight()
        self.tts.stop()
        self._pending_utterance.clear()
        self.vad.reset()
        await self.broadcast("cancelled", reason=reason)
        await self._set_state(State.IDLE)
        if listen_after:
            await self._begin_listening()

    def _nudge_lines_for_text(self, text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        t = (text or "").strip()
        if _CAMERA_PROMPT_RE.search(t):
            ack = (
                "Okay, checking the camera now.",
                "Understood, looking through the camera.",
                "Alright, let me check what the camera sees.",
            )
            progress = (
                "Still looking at the camera view.",
                "Hang on, still checking the camera details.",
                "Almost there, still processing the camera frame.",
            )
            return ack, progress
        if _SCREEN_PROMPT_RE.search(t):
            ack = (
                "Okay, checking your screen.",
                "Understood, looking at your screen now.",
                "Alright, let me see what's on screen.",
            )
            progress = (
                "Still checking your screen.",
                "One sec, still going through what's on screen.",
                "Still working through the screen details.",
            )
            return ack, progress
        if _COMMAND_PROMPT_RE.search(t):
            ack = (
                "On it, running that now.",
                "Yep, doing that now.",
                "Alright, running it now.",
            )
            progress = (
                "Still working on that.",
                "One sec, that's still running.",
                "Still on it, almost done.",
            )
            return ack, progress
        ack = (
            "On it.",
            "Understood, working on it.",
            "Alright, let me handle that.",
        )
        progress = (
            "Still working on it.",
            "One sec, still processing this.",
            "Still on it, just taking a moment.",
            "Almost there, wrapping this up.",
            "Hang tight, still working through it.",
            "Still going, checking the details.",
            "Yup, still in progress.",
            "Still on this, thanks for waiting.",
            "Taking a bit longer, but I'm on it.",
            "Still working through it carefully.",
            "Haven't forgotten, still processing.",
            "Still in progress, almost there.",
        )
        return ack, progress

    def _arm_silence_nudge(
        self,
        turn_id: int,
        *,
        delay_s: float = 1.0,
        source_text: str = "",
    ) -> None:
        if self._silence_nudge_task is not None:
            self._silence_nudge_task.cancel()
            self._silence_nudge_task = None
        self._turn_last_progress_at[turn_id] = time.monotonic()
        ack_lines, progress_lines = self._nudge_lines_for_text(source_text)

        async def _later() -> None:
            try:
                await asyncio.sleep(max(0.1, delay_s))
                if turn_id != self._turn_id or self.state != State.THINKING:
                    return
                # If another status line already spoke (e.g. "Oops, that did not
                # work. Let me try fixing it."), treat that as first anti-silence
                # narration and skip the default acknowledgement.
                if turn_id not in self._turn_progress_announced:
                    self._turn_progress_announced.add(turn_id)
                    self._turn_last_progress_at[turn_id] = time.monotonic()
                    ack = ack_lines[turn_id % len(ack_lines)]
                    await self._think_speak(turn_id, ack)
                # Keep speaking periodic progress while we're still thinking.
                while True:
                    if turn_id != self._turn_id:
                        return
                    if self.state != State.THINKING:
                        return
                    last = self._turn_last_progress_at.get(turn_id, 0.0)
                    if (time.monotonic() - last) >= 50.0:
                        self._turn_progress_announced.add(turn_id)
                        self._turn_last_progress_at[turn_id] = time.monotonic()
                        idx = self._turn_progress_idx.get(turn_id, 0)
                        line = progress_lines[idx % len(progress_lines)]
                        self._turn_progress_idx[turn_id] = idx + 1
                        await self._think_speak(turn_id, line)
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return

        self._silence_nudge_task = asyncio.create_task(_later())

    async def _process_utterance(self) -> None:
        turn_id = self._turn_id
        await self._set_state(State.THINKING)
        audio = np.concatenate(self._pending_utterance) if self._pending_utterance \
            else np.zeros(0, dtype=np.int16)
        self._pending_utterance.clear()

        if self._enroll is not None:
            collected, target = self.speaker.enroll_add(self._enroll.name, audio)
            self._enroll.collected = collected
            await self.broadcast("enroll_progress", name=self._enroll.name,
                                 collected=collected, target=target)
            if collected >= target:
                refine = self._enroll.refine
                name = self._enroll.name
                self._enroll = None
                ok = self.speaker.enroll_finalize(name, refine=refine)
                if ok:
                    await self.broadcast("profiles",
                                         items=list(self.speaker.profiles()))
                    await self.broadcast("settings",
                                         **self._settings_snapshot())
                await self.broadcast("enroll_done", name=name, ok=ok,
                                     refine=refine)
                if ok:
                    reply = (
                        f"I updated your voice profile for {name} with more "
                        f"training data."
                        if refine else
                        f"Enrolled your voice as {name}."
                    )
                else:
                    reply = "Enrollment failed, try again with longer phrases."
                if turn_id == self._turn_id:
                    await self._speak(reply, turn_id=turn_id)
            else:
                if turn_id == self._turn_id:
                    await self._speak(
                        f"Sample {collected} of {target}. Say another sentence.",
                        turn_id=turn_id,
                    )
                    self._enroll_continue()
            return

        text = await asyncio.to_thread(self.stt.transcribe, audio)
        if turn_id != self._turn_id:
            log.info("discarding transcript from interrupted turn")
            return
        user, score = ("guest", 0.0)
        if self.cfg.speaker_id.enabled:
            user, score = await asyncio.to_thread(self.speaker.identify, audio)
        if turn_id != self._turn_id:
            log.info("discarding speaker result from interrupted turn")
            return

        await self.broadcast("transcript", text=text, user=user, score=score)
        if not text:
            await self._set_state(State.IDLE)
            return

        def on_status(msg: str) -> None:
            def _schedule() -> None:
                self._turn_progress_announced.add(turn_id)
                self._turn_last_progress_at[turn_id] = time.monotonic()
                asyncio.create_task(self._think_speak(turn_id, msg))

            self.loop.call_soon_threadsafe(_schedule)

        self._arm_silence_nudge(turn_id, delay_s=1.0, source_text=text)
        result = await asyncio.to_thread(
            self.orch.handle, text, user, on_status
        )
        if self._silence_nudge_task is not None:
            self._silence_nudge_task.cancel()
            self._silence_nudge_task = None
        if turn_id != self._turn_id:
            log.info("discarding LLM result from interrupted turn")
            return
        clean_reply = _clean_for_speech(result.reply)
        if result.intent == "start_live_vision":
            ok, msg = await self._start_live_camera()
            clean_reply = msg
            result.success = ok
        elif result.intent == "stop_live_vision":
            _ok, msg = await self._stop_live_camera()
            clean_reply = msg
        if isinstance(result.data, dict):
            b64 = str(result.data.get("camera_frame_b64") or "").strip()
            if b64:
                await self.broadcast(
                    "camera_frame",
                    image_b64=b64,
                    mime=str(result.data.get("camera_mime") or "image/jpeg"),
                )
        await self.broadcast("reply", text=clean_reply, intent=result.intent,
                             success=result.success)
        if result.intent == "propose_patch":
            patches = await asyncio.to_thread(self.patches.list_patches)
            await self.broadcast("patches", items=patches)
        if turn_id == self._turn_id:
            await self._speak(clean_reply, turn_id=turn_id)

    async def _process_text_prompt(self, text: str, user: str = "guest") -> None:
        """Handle typed HUD prompts through the same orchestration path as voice."""
        text = (text or "").strip()
        if not text:
            return
        self._turn_id += 1
        turn_id = self._turn_id
        self._turn_progress_announced = {
            t for t in self._turn_progress_announced if t >= turn_id - 4
        }
        self._turn_last_progress_at = {
            t: ts for t, ts in self._turn_last_progress_at.items()
            if t >= turn_id - 4
        }
        self._turn_progress_idx = {
            t: idx for t, idx in self._turn_progress_idx.items()
            if t >= turn_id - 4
        }
        self.tts.stop()
        await self._set_state(State.THINKING)
        await self.broadcast("transcript", text=text, user=user, score=1.0)

        def on_status(msg: str) -> None:
            def _schedule() -> None:
                self._turn_progress_announced.add(turn_id)
                self._turn_last_progress_at[turn_id] = time.monotonic()
                asyncio.create_task(self._think_speak(turn_id, msg))

            self.loop.call_soon_threadsafe(_schedule)

        self._arm_silence_nudge(turn_id, delay_s=1.0, source_text=text)
        result = await asyncio.to_thread(self.orch.handle, text, user, on_status)
        if self._silence_nudge_task is not None:
            self._silence_nudge_task.cancel()
            self._silence_nudge_task = None
        if turn_id != self._turn_id:
            log.info("discarding text prompt result from interrupted turn")
            return
        clean_reply = _clean_for_speech(result.reply)
        if result.intent == "start_live_vision":
            ok, msg = await self._start_live_camera()
            clean_reply = msg
            result.success = ok
        elif result.intent == "stop_live_vision":
            _ok, msg = await self._stop_live_camera()
            clean_reply = msg
        if isinstance(result.data, dict):
            b64 = str(result.data.get("camera_frame_b64") or "").strip()
            if b64:
                await self.broadcast(
                    "camera_frame",
                    image_b64=b64,
                    mime=str(result.data.get("camera_mime") or "image/jpeg"),
                )
        await self.broadcast("reply", text=clean_reply, intent=result.intent,
                             success=result.success)
        if result.intent == "propose_patch":
            patches = await asyncio.to_thread(self.patches.list_patches)
            await self.broadcast("patches", items=patches)
        await self._speak(clean_reply, turn_id=turn_id)

    def _enroll_continue(self) -> None:
        async def _later() -> None:
            while self.tts.is_speaking():
                await asyncio.sleep(0.05)
            await self._begin_listening()

        asyncio.create_task(_later())

    async def _think_speak(self, turn_id: int, text: str) -> None:
        """Brief TTS + HUD line while still in THINKING (LLM running in thread)."""
        spoken = _clean_for_speech(text)
        if turn_id != self._turn_id or not spoken.strip():
            return
        async with self._narration_lock:
            if turn_id != self._turn_id:
                return
            self._turn_last_progress_at[turn_id] = time.monotonic()
            await self.broadcast("narration_start", text=spoken)
            done = asyncio.Event()

            def on_end() -> None:
                self.loop.call_soon_threadsafe(done.set)

            self.tts.speak(spoken.strip(), on_end=on_end)
            await done.wait()
            if turn_id != self._turn_id:
                return
            await self.broadcast("narration_end")

    async def _speak(self, text: str, *, turn_id: int | None = None) -> None:
        text = _clean_for_speech(text)
        if turn_id is not None and turn_id != self._turn_id:
            return
        if not text:
            await self._set_state(State.IDLE)
            return
        # Serialize final reply speech behind thinking narration to avoid
        # overlapped audio when a status line and final reply race.
        async with self._narration_lock:
            await self._set_state(State.SPEAKING)
            await self.broadcast("speaking_start", text=text)
            done = asyncio.Event()

            def on_end() -> None:
                self.loop.call_soon_threadsafe(done.set)

            self.tts.speak(text, on_end=on_end)
            await done.wait()
        if turn_id is not None and turn_id != self._turn_id:
            return
        await self.broadcast("speaking_end")
        await self._set_state(State.IDLE)

    async def _audio_loop(self) -> None:
        max_samples = int(self.cfg.audio.sample_rate * self.cfg.vad.max_utterance_s)
        while True:
            frame = await self.audio.frames.get()
            if self.muted:
                continue
            if self.state == State.IDLE:
                if self.wake.feed(frame):
                    await self.broadcast("wake")
                    await self._begin_listening()
                continue
            if self.state == State.LISTENING:
                self._pending_utterance.append(frame)
                active, eou = self.vad.feed(frame)
                total = sum(a.shape[0] for a in self._pending_utterance)
                too_long = total >= max_samples
                silence_timeout = (
                    not active
                    and (time.monotonic() - self._utter_started) > 6.0
                    and self.vad.speech_ms == 0
                )
                if eou or too_long or silence_timeout:
                    log.info("end of utterance (eou=%s too_long=%s timeout=%s)",
                             eou, too_long, silence_timeout)
                    asyncio.create_task(self._process_utterance())
            if self.state in (State.THINKING, State.SPEAKING):
                if self.wake.feed(frame):
                    log.info("wake word barge-in while %s", self.state.value)
                    await self.broadcast("wake")
                    await self._interrupt_current("wake", listen_after=True)

    async def run(self) -> None:
        # Pull any missing Ollama models (vision + main) in the background.
        def _on_ollama_bootstrap_done() -> None:
            self.loop.call_soon_threadsafe(
                lambda: self.loop.create_task(self._after_ollama_bootstrap()),
            )

        start_ollama_bootstrap_thread(self.cfg, on_complete=_on_ollama_bootstrap_done)
        self.audio.start()
        # `Get-StartApps` (first open) can take many seconds; load in the
        # background so the first "open" command does not block on it.
        asyncio.get_running_loop().run_in_executor(
            None, prewarm_start_menu_cache)
        broadcaster = asyncio.create_task(self._broadcaster())
        audio_task = asyncio.create_task(self._audio_loop())
        # Prime the ElevenLabs voice cache if we already have a key.
        asyncio.create_task(self._refresh_elevenlabs_voices())
        log.info("starting WebSocket server on ws://%s:%d",
                 self.cfg.server.host, self.cfg.server.port)
        async with websockets.serve(
            self.handle_client, self.cfg.server.host, self.cfg.server.port
        ):
            try:
                await asyncio.Future()
            finally:
                await self._stop_live_camera()
                audio_task.cancel()
                broadcaster.cancel()
                self.tts.stop()
                self.audio.stop()
