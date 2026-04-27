"""Speech-to-text using faster-whisper."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .config import SttCfg

log = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self, cfg: SttCfg, cache_dir: Path | None = None):
        self.cfg = cfg
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper not installed. Run `pip install faster-whisper`."
            ) from e

        device = cfg.device
        if device == "auto":
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        compute_type = cfg.compute_type
        if device == "cpu" and compute_type == "float16":
            compute_type = "int8"
        log.info(
            "loading faster-whisper model=%s device=%s compute=%s",
            cfg.model,
            device,
            compute_type,
        )
        self._model = WhisperModel(
            cfg.model,
            device=device,
            compute_type=compute_type,
            download_root=str(cache_dir) if cache_dir else None,
        )

    def transcribe(self, audio_int16: np.ndarray, sample_rate: int = 16000) -> str:
        if audio_int16.size == 0:
            return ""
        pcm = audio_int16.astype(np.float32) / 32768.0
        if sample_rate != 16000:
            raise ValueError("whisper expects 16 kHz input")
        segments, _info = self._model.transcribe(
            pcm,
            language="en",
            beam_size=self.cfg.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments).strip()
        log.info("transcribed: %r", text)
        return text
