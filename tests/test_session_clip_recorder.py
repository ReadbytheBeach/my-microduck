#!/usr/bin/env python3
"""SessionClipRecorder tests — real ffmpeg, synthetic frames, no Isaac."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from session_clip_recorder import SessionClipRecorder  # noqa: E402

WIDTH, HEIGHT, FPS = 64, 48, 10
FRAME = bytes(WIDTH * HEIGHT * 3)  # black rgb24 frame


class FakeFeed:
    """Minimal stand-in for playback_command_feed.CommandFeed."""

    def __init__(self):
        self.applied_command_id: str | None = None
        self.results: dict[str, dict] = {}

    def finish(self, command_id: str, status: str = "completed") -> None:
        self.results[command_id] = {"status": status, "command_id": command_id}
        self.applied_command_id = None


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class SessionClipRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clips_dir = Path(self.tmp.name) / "clips"

    def tearDown(self):
        self.tmp.cleanup()

    def make_recorder(self, **kwargs) -> SessionClipRecorder:
        kwargs.setdefault("encoder", "libx264")  # GPU-free in tests
        return SessionClipRecorder(self.clips_dir, WIDTH, HEIGHT, FPS, **kwargs)

    def test_clip_per_command(self):
        feed = FakeFeed()
        recorder = self.make_recorder()
        feed.applied_command_id = "abcdef1234567890"
        recorder.poll(feed)
        for _ in range(30):
            recorder.write_frame(FRAME)
        feed.finish("abcdef1234567890")
        recorder.poll(feed)
        recorder.close()
        clips = sorted(self.clips_dir.glob("*.mp4"))
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].name.startswith("0001_abcdef12"))
        self.assertGreater(clips[0].stat().st_size, 0)

    def test_short_clip_suppressed(self):
        feed = FakeFeed()
        recorder = self.make_recorder(min_frames=12)
        feed.applied_command_id = "short00000000001"
        recorder.poll(feed)
        for _ in range(3):  # 0.3s at 10fps: closed-loop supersede churn
            recorder.write_frame(FRAME)
        feed.finish("short00000000001")
        recorder.poll(feed)
        self.assertEqual(sorted(self.clips_dir.glob("*.mp4")), [])
        # index still advances: the next real clip is 0002
        feed.applied_command_id = "longer0000000000"
        recorder.poll(feed)
        for _ in range(30):
            recorder.write_frame(FRAME)
        feed.finish("longer0000000000")
        recorder.poll(feed)
        recorder.close()
        clips = sorted(self.clips_dir.glob("*.mp4"))
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].name.startswith("0002_longer00"))

    def test_supersede_closes_previous(self):
        feed = FakeFeed()
        recorder = self.make_recorder()
        feed.applied_command_id = "first000000000001"
        recorder.poll(feed)
        for _ in range(20):
            recorder.write_frame(FRAME)
        # new command supersedes without a results entry for the old one
        feed.applied_command_id = "second00000000002"
        recorder.poll(feed)
        for _ in range(20):
            recorder.write_frame(FRAME)
        recorder.close()
        clips = sorted(self.clips_dir.glob("*.mp4"))
        self.assertEqual(len(clips), 2)
        self.assertEqual([c.name[:5] for c in clips], ["0001_", "0002_"])


if __name__ == "__main__":
    unittest.main()
