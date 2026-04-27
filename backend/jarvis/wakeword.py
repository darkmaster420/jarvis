"""Wake-word detection using openWakeWord.

Supports any number of wake words at once: each entry in
``WakeWordCfg.models`` is passed to openWakeWord, and a detection counts if
*any* model crosses its threshold (with optional per-model overrides).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np

from .config import WakeWordCfg

log = logging.getLogger(__name__)

# openWakeWord expects 16 kHz int16 mono, 80 ms chunks (1280 samples).
OWW_CHUNK = 1280
OWW_RATE = 16000


class WakeWord:
    """Thin wrapper around openWakeWord's ``Model`` that handles a list of
    wake words and per-word thresholds."""

    def __init__(self, cfg: WakeWordCfg):
        self.cfg = cfg
        try:
            from openwakeword.model import Model
            from openwakeword import MODELS as OWW_BUILTINS  # dict name -> metadata
        except ImportError as e:
            raise RuntimeError(
                "openwakeword not installed. Run `pip install openwakeword`."
            ) from e

        requested = [m for m in (cfg.models or []) if m]
        if not requested:
            requested = ["hey_jarvis"]
            log.warning("wake_word.models was empty; defaulting to hey_jarvis")

        # Validate each entry. openWakeWord accepts either the built-in name
        # (which must exist in its MODELS registry) or a path to an .onnx file.
        # We skip bad entries with a clear warning rather than letting the
        # whole backend die on a typo.
        models: list[str] = []
        skipped: list[str] = []
        for m in requested:
            looks_like_path = os.path.sep in m or m.endswith(".onnx")
            if looks_like_path:
                if Path(m).exists():
                    models.append(m)
                else:
                    skipped.append(f"{m} (file not found)")
                continue
            if m in OWW_BUILTINS:
                models.append(m)
            else:
                skipped.append(
                    f"{m} (not a built-in; expected one of: "
                    f"{', '.join(sorted(OWW_BUILTINS))})"
                )

        if skipped:
            log.warning(
                "skipping unknown wake word models: %s. "
                "Custom wake words need a trained *.onnx file; "
                "see openWakeWord docs for training.",
                " | ".join(skipped),
            )

        if not models:
            log.error(
                "no valid wake word models after filtering %r; falling back to "
                "hey_jarvis so the backend can still start", requested)
            models = ["hey_jarvis"]

        # Take a snapshot of the user-facing names before handing the list to
        # openWakeWord - it will rewrite the entries to versioned filenames
        # (e.g. "hey_jarvis" -> "hey_jarvis_v0.1").
        configured_names = [
            Path(m).stem if (os.path.sep in m or m.endswith(".onnx")) else m
            for m in models
        ]
        log.info("loading wake word models: %s", ", ".join(configured_names))
        self._model = Model(
            wakeword_models=list(models),
            inference_framework="onnx",
        )

        # openWakeWord keys scores by whatever name appears in the resolved
        # model file. Build a threshold map that covers both the user-facing
        # names (so config overrides by friendly name work) and the resolved
        # names (so per-score lookups work at runtime).
        resolved_names = list(getattr(self._model, "models", {}).keys())
        log.info("wake word models ready: %s", ", ".join(resolved_names))
        overrides = cfg.thresholds or {}
        self._thresholds: dict[str, float] = {}
        # Defaults for every resolved name.
        for name in resolved_names:
            self._thresholds[name] = float(cfg.threshold)
        # Apply overrides matched either exactly or by prefix (friendly name).
        for key, thr in overrides.items():
            t = float(thr)
            self._thresholds[key] = t
            for name in resolved_names:
                if name == key or name.startswith(key + "_"):
                    self._thresholds[name] = t

        self._buffer = np.zeros(0, dtype=np.int16)
        self._last_trigger = 0.0
        self._last_name: str | None = None

    def feed(self, samples: np.ndarray) -> bool:
        """Feed samples. Returns True on detection (respecting cooldown)."""
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        self._buffer = np.concatenate([self._buffer, samples])
        triggered = False
        while self._buffer.shape[0] >= OWW_CHUNK:
            chunk = self._buffer[:OWW_CHUNK]
            self._buffer = self._buffer[OWW_CHUNK:]
            scores = self._model.predict(chunk)
            if self._check(scores):
                triggered = True
        return triggered

    def _check(self, scores: dict) -> bool:
        now = time.monotonic()
        if now - self._last_trigger < self.cfg.cooldown_s:
            return False
        for name, score in scores.items():
            threshold = self._thresholds.get(name, self.cfg.threshold)
            if score >= threshold:
                log.info("wake word '%s' detected (score=%.2f, thr=%.2f)",
                         name, score, threshold)
                self._last_trigger = now
                self._last_name = name
                self._model.reset()
                return True
        return False

    @property
    def last_detected(self) -> str | None:
        """Name of the wake word from the most recent successful trigger.
        Useful for per-wake-word routing down the line."""
        return self._last_name

    def reset(self) -> None:
        self._model.reset()
        self._buffer = np.zeros(0, dtype=np.int16)
