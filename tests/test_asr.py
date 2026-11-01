#!/usr/bin/env python3
"""ASR module tests — sherpa-onnx paraformer-zh, synthetic + bundled WAVs only.

Never touches a sound card. Skipped when the model has not been downloaded
to work/language_control/asr_model/.
"""

from __future__ import annotations

import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from language_control.asr import DEFAULT_MODEL_DIR, Asr, AsrError  # noqa: E402

MODEL_READY = (DEFAULT_MODEL_DIR / "model.int8.onnx").is_file()


def write_sine_wav(path: Path, seconds: float = 1.0, freq: float = 440.0) -> None:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 16000)))
            for i in range(frames)
        ))


@unittest.skipUnless(MODEL_READY, "ASR model not downloaded")
class AsrTests(unittest.TestCase):
    def test_synthetic_wav_returns_dict_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "sine.wav"
            write_sine_wav(wav, seconds=0.5)
            result = Asr().transcribe(wav)
        self.assertIn("text", result)
        self.assertAlmostEqual(result["duration_s"], 0.5, places=2)
        self.assertEqual(len(result["model_sha256"]), 64)

    def test_bundled_test_wav_transcribes_to_chinese(self):
        wav = DEFAULT_MODEL_DIR / "test_wavs" / "0.wav"
        if not wav.is_file():
            self.skipTest("bundled test wav not downloaded")
        result = Asr().transcribe(wav)
        self.assertIn("研究", result["text"])

    def test_wrong_sample_rate_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "bad.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000)
            with self.assertRaises(AsrError):
                Asr().transcribe(wav)


if __name__ == "__main__":
    unittest.main()
