"""Jarvis core server.

Owns the assistant state machine and runs a WebSocket endpoint the HUD
clients connect to. States: idle -> listening -> thinking -> speaking.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
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
from .skills.system import prewarm_start_menu_cache

log = logging.getLogger(__name__)


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
        # False after background ensure_ollama_models() returns (HUD can show Ollama status).
        self._ollama_bootstrap_pending = True

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
        elif cmd == "list_settings":
            await self.broadcast("settings", **self._settings_snapshot())
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
                            "Restart me to pick up core changes."
                        )
                    await self.broadcast("patch_applied", id=info["id"],
                                         target=info["applied"])
                    patches = await asyncio.to_thread(self.patches.list_patches)
                    await self.broadcast("patches", items=patches)
                    await self._speak(speak)
                except Exception as e:
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
        self.tts.stop()
        self._pending_utterance.clear()
        self.vad.reset()
        await self.broadcast("cancelled", reason=reason)
        await self._set_state(State.IDLE)
        if listen_after:
            await self._begin_listening()

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
                asyncio.create_task(self._think_speak(turn_id, msg))

            self.loop.call_soon_threadsafe(_schedule)

        result = await asyncio.to_thread(
            self.orch.handle, text, user, on_status
        )
        if turn_id != self._turn_id:
            log.info("discarding LLM result from interrupted turn")
            return
        await self.broadcast("reply", text=result.reply, intent=result.intent,
                             success=result.success)
        if result.intent == "propose_patch":
            patches = await asyncio.to_thread(self.patches.list_patches)
            await self.broadcast("patches", items=patches)
        if turn_id == self._turn_id:
            await self._speak(result.reply, turn_id=turn_id)

    def _enroll_continue(self) -> None:
        async def _later() -> None:
            while self.tts.is_speaking():
                await asyncio.sleep(0.05)
            await self._begin_listening()

        asyncio.create_task(_later())

    async def _think_speak(self, turn_id: int, text: str) -> None:
        """Brief TTS + HUD line while still in THINKING (LLM running in thread)."""
        if turn_id != self._turn_id or not text.strip():
            return
        async with self._narration_lock:
            if turn_id != self._turn_id:
                return
            await self.broadcast("narration_start", text=text)
            done = asyncio.Event()

            def on_end() -> None:
                self.loop.call_soon_threadsafe(done.set)

            self.tts.speak(text.strip(), on_end=on_end)
            await done.wait()
            if turn_id != self._turn_id:
                return
            await self.broadcast("narration_end")

    async def _speak(self, text: str, *, turn_id: int | None = None) -> None:
        if turn_id is not None and turn_id != self._turn_id:
            return
        if not text:
            await self._set_state(State.IDLE)
            return
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
                audio_task.cancel()
                broadcaster.cancel()
                self.tts.stop()
                self.audio.stop()
