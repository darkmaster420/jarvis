"""Microphone capture and ring buffer for Jarvis.

Provides a background `AudioCapture` that feeds fixed-size int16 frames into
an asyncio queue, plus a lock-free ring buffer of recent samples for
pre-roll / VAD lookback.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Deque

import numpy as np
import sounddevice as sd

from .config import AudioCfg

log = logging.getLogger(__name__)


class RingBuffer:
    """Thread-safe int16 PCM ring buffer (mono)."""

    def __init__(self, sample_rate: int, seconds: float):
        self.sample_rate = sample_rate
        self.capacity = int(sample_rate * seconds)
        self._buf: Deque[np.ndarray] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def write(self, samples: np.ndarray) -> None:
        with self._lock:
            self._buf.append(samples.copy())
            self._size += samples.shape[0]
            while self._size > self.capacity and self._buf:
                drop = self._buf.popleft()
                overflow = self._size - self.capacity
                if overflow >= drop.shape[0]:
                    self._size -= drop.shape[0]
                else:
                    keep = drop[overflow:]
                    self._buf.appendleft(keep)
                    self._size -= overflow
                    break

    def read_last(self, seconds: float) -> np.ndarray:
        n = int(self.sample_rate * seconds)
        with self._lock:
            if not self._buf:
                return np.zeros(0, dtype=np.int16)
            data = np.concatenate(list(self._buf))
        if data.shape[0] <= n:
            return data
        return data[-n:]


class AudioCapture:
    """Asyncio-friendly microphone capture at a fixed frame size."""

    def __init__(self, cfg: AudioCfg, loop: asyncio.AbstractEventLoop | None = None):
        self.cfg = cfg
        self.loop = loop or asyncio.get_running_loop()
        self.frame_samples = int(cfg.sample_rate * cfg.frame_ms / 1000)
        self.frames: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=200)
        self.ring = RingBuffer(cfg.sample_rate, seconds=5.0)
        self._stream: sd.InputStream | None = None
        self._dropped = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            log.debug("sounddevice status: %s", status)
        samples = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        if samples.dtype != np.int16:
            samples = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
        self.ring.write(samples)
        try:
            self.loop.call_soon_threadsafe(self._push, samples)
        except RuntimeError:
            pass

    def _push(self, samples: np.ndarray) -> None:
        try:
            self.frames.put_nowait(samples)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.warning("audio queue full, dropped %d frames", self._dropped)

    def start(self) -> None:
        if self._stream is not None:
            return
        log.info(
            "opening mic: sr=%d frame=%dms (%d samples) device=%s",
            self.cfg.sample_rate,
            self.cfg.frame_ms,
            self.frame_samples,
            self.cfg.input_device,
        )
        self._stream = sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self.frame_samples,
            device=self.cfg.input_device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def aclose(self) -> None:
        self.stop()
