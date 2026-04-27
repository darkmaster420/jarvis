"""Voice-activity detection via silero-vad.

Acts as an end-of-utterance detector: accept chunks, maintain a running state
of "is voice active", and report when N ms of silence have passed after speech.
"""
from __future__ import annotations

import logging

import numpy as np

from .config import VadCfg

log = logging.getLogger(__name__)

# silero-vad operates on 16 kHz, 512-sample (32 ms) chunks.
SILERO_CHUNK = 512
SILERO_RATE = 16000


class VoiceActivityDetector:
    def __init__(self, cfg: VadCfg):
        self.cfg = cfg
        try:
            from silero_vad import load_silero_vad
            import torch  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "silero-vad not installed. Run `pip install silero-vad`."
            ) from e

        log.info("loading silero-vad")
        self._model = load_silero_vad()
        import torch

        self._torch = torch
        self._buffer = np.zeros(0, dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self._speech_ms = 0
        self._silence_ms = 0
        self._in_speech = False
        self._buffer = np.zeros(0, dtype=np.float32)
        try:
            self._model.reset_states()
        except Exception:  # pragma: no cover
            pass

    def feed(self, samples_int16: np.ndarray) -> tuple[bool, bool]:
        """Feed 16 kHz mono int16 samples.

        Returns (speech_active_now, end_of_utterance).
        end_of_utterance is True exactly once when min_silence_ms of silence
        follows at least one detected speech chunk.
        """
        pcm = samples_int16.astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, pcm])
        eou = False
        frame_ms = int(SILERO_CHUNK * 1000 / SILERO_RATE)
        while self._buffer.shape[0] >= SILERO_CHUNK:
            chunk = self._buffer[:SILERO_CHUNK]
            self._buffer = self._buffer[SILERO_CHUNK:]
            t = self._torch.from_numpy(chunk)
            with self._torch.no_grad():
                prob = float(self._model(t, SILERO_RATE).item())
            if prob >= self.cfg.threshold:
                self._in_speech = True
                self._speech_ms += frame_ms
                self._silence_ms = 0
            else:
                if self._in_speech:
                    self._silence_ms += frame_ms
                    if self._silence_ms >= self.cfg.min_silence_ms:
                        eou = True
                        self._in_speech = False
                        self._silence_ms = 0
        return self._in_speech, eou

    @property
    def speech_ms(self) -> int:
        return self._speech_ms
