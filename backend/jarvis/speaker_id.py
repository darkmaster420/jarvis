"""Speaker identification via Resemblyzer.

Stores per-user embeddings as `.npy` files in `profiles/`. At runtime, each
incoming utterance is embedded and compared against all profiles using cosine
similarity; the best match above `threshold` wins, otherwise the speaker is
reported as `"guest"`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import numpy as np

from .config import SpeakerIdCfg

log = logging.getLogger(__name__)

GUEST = "guest"


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class SpeakerID:
    def __init__(self, cfg: SpeakerIdCfg, profiles_dir: Path):
        self.cfg = cfg
        self.profiles_dir = profiles_dir
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        try:
            from resemblyzer import VoiceEncoder, preprocess_wav
        except ImportError as e:
            raise RuntimeError(
                "resemblyzer not installed. Run `pip install resemblyzer`."
            ) from e

        log.info("loading resemblyzer encoder")
        self._encoder = VoiceEncoder("cpu")
        self._preprocess = preprocess_wav
        self._profiles: dict[str, np.ndarray] = {}
        self._enroll_stash: dict[str, list[np.ndarray]] = {}
        self._load_profiles()

    def _min_audio_samples(self) -> int:
        s = max(0.4, min(5.0, float(self.cfg.enroll_min_utterance_s)))
        return int(s * 16000)

    def enroll_target(self) -> int:
        return max(2, min(32, int(self.cfg.enroll_samples)))

    def has_profile(self, name: str) -> bool:
        n = (name or "").strip().lower()
        return bool(n) and n in self._profiles

    def _load_profiles(self) -> None:
        self._profiles.clear()
        for p in self.profiles_dir.glob("*.npy"):
            try:
                emb = np.load(p)
                self._profiles[p.stem.lower()] = emb
            except Exception as e:
                log.warning("failed to load profile %s: %s", p, e)
        if self._profiles:
            log.info("loaded %d speaker profile(s): %s",
                     len(self._profiles), ", ".join(self._profiles))

    def _embed(self, audio_int16: np.ndarray, sr: int = 16000) -> np.ndarray | None:
        if audio_int16.size < self._min_audio_samples():
            return None
        pcm = audio_int16.astype(np.float32) / 32768.0
        wav = self._preprocess(pcm, source_sr=sr)
        return self._encoder.embed_utterance(wav)

    def identify(self, audio_int16: np.ndarray, sr: int = 16000) -> tuple[str, float]:
        if not self.cfg.enabled or not self._profiles:
            return GUEST, 0.0
        emb = self._embed(audio_int16, sr)
        if emb is None:
            return GUEST, 0.0
        best_name, best_score = GUEST, 0.0
        for name, ref in self._profiles.items():
            score = _cosine(emb, ref)
            if score > best_score:
                best_name, best_score = name, score
        if best_score < self.cfg.threshold:
            return GUEST, best_score
        return best_name, best_score

    def enroll_add(
        self, name: str, audio_int16: np.ndarray, sr: int = 16000
    ) -> tuple[int, int]:
        """Add a sample for `name`. Returns (collected, target)."""
        name = name.strip().lower()
        if not name or name == GUEST:
            raise ValueError("invalid profile name")
        tgt = self.enroll_target()
        emb = self._embed(audio_int16, sr)
        if emb is None:
            return len(self._enroll_stash.get(name, [])), tgt
        self._enroll_stash.setdefault(name, []).append(emb)
        return len(self._enroll_stash[name]), tgt

    def enroll_finalize(self, name: str, *, refine: bool = False) -> bool:
        """Write profile from stash. If ``refine`` and a profile already exists,
        the old centroid and the new session samples are averaged (old counts
        as one voiceprint + each new utterance) so the model 'learns' more."""
        name = name.strip().lower()
        samples = self._enroll_stash.get(name, [])
        if len(samples) < 2:
            return False
        batch = np.stack(samples, axis=0)  # (n, d)
        new_mean = np.mean(batch, axis=0)
        if refine and name in self._profiles:
            old = self._profiles[name].reshape(1, -1)  # (1, d)
            # Old embedding + n new ones → new centroid; improves stability vs
            # replacing the profile with only the latest session.
            merged = np.mean(np.vstack((old, batch)), axis=0)
            log.info(
                "refined speaker '%s' (merged prior profile with %d new utterance(s)) -> %s",
                name, len(samples), self.profiles_dir / f"{name}.npy",
            )
            out = merged
        else:
            out = new_mean
            log.info("enrolled speaker '%s' (avg of %d samples) -> %s",
                     name, len(samples), self.profiles_dir / f"{name}.npy")
        path = self.profiles_dir / f"{name}.npy"
        np.save(path, out)
        self._profiles[name] = out
        self._enroll_stash.pop(name, None)
        return True

    def profiles(self) -> Iterable[str]:
        return list(self._profiles.keys())

    def delete_profile(self, name: str) -> bool:
        name = (name or "").strip().lower()
        if not name:
            return False
        removed = False
        path = self.profiles_dir / f"{name}.npy"
        if path.exists():
            try:
                path.unlink()
                removed = True
            except Exception as e:
                log.warning("could not delete %s: %s", path, e)
                return False
        self._profiles.pop(name, None)
        self._enroll_stash.pop(name, None)
        return removed

    def cancel_enroll(self, name: str | None = None) -> None:
        if name:
            self._enroll_stash.pop(name.strip().lower(), None)
        else:
            self._enroll_stash.clear()
