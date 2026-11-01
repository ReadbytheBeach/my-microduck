#!/usr/bin/env python3
"""Offline ASR via sherpa-onnx paraformer-zh (M2 web voice input).

Model lives in work/language_control/asr_model/ (downloaded via hf-mirror,
SHA-256 recorded at load and logged with every asr ledger event). Input is
16 kHz mono s16 WAV — the web layer transcodes browser webm/opus with
ffmpeg before calling this module. Tests use synthetic WAVs and never touch
a sound card.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import wave

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "work/language_control/asr_model"


class AsrError(RuntimeError):
    pass


class Asr:
    """Lazy-loading offline recognizer; one instance per server process."""

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR):
        self.model_dir = Path(model_dir)
        self._recognizer = None
        self.model_sha256: str | None = None

    def _load(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        model = self.model_dir / "model.int8.onnx"
        if not model.is_file():
            model = self.model_dir / "model.onnx"
        tokens = self.model_dir / "tokens.txt"
        if not model.is_file() or not tokens.is_file():
            raise AsrError(
                f"ASR model not found in {self.model_dir} "
                f"(need model[.int8].onnx + tokens.txt; see 语音输入方案文档)"
            )
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(model),
            tokens=str(tokens),
            num_threads=4,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
        )
        self.model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()

    def transcribe(self, wav_path: Path) -> dict:
        """Transcribe a 16 kHz mono s16 WAV. Returns {text, duration_s, model_sha256}."""
        self._load()
        try:
            with wave.open(str(wav_path), "rb") as wav:
                if wav.getframerate() != 16000 or wav.getnchannels() != 1 \
                        or wav.getsampwidth() != 2:
                    raise AsrError(
                        f"WAV must be 16kHz mono s16, got "
                        f"{wav.getframerate()}Hz/{wav.getnchannels()}ch/"
                        f"{wav.getsampwidth()}B"
                    )
                frames = wav.readframes(wav.getnframes())
                duration_s = wav.getnframes() / 16000.0
        except wave.Error as exc:
            raise AsrError(f"invalid WAV: {exc}") from exc
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self._recognizer.decode_stream(stream)
        return {
            "text": stream.result.text.strip(),
            "duration_s": round(duration_s, 3),
            "model_sha256": self.model_sha256,
        }
