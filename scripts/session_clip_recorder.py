#!/usr/bin/env python3
"""Per-command mp4 clip recorder for session-mode playback (M2 web video).

Isaac-free, stdlib only. The playback loop feeds it camera frames and polls
it with the CommandFeed; a newly applied command opens an ffmpeg pipe, a
finished/superseded command closes it. Clips shorter than ``min_frames``
(0-indexed by written frames) are deleted — closed-loop control supersedes
command ids mid-goal and would otherwise spam sub-second fragments.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


def ffmpeg_rawvideo_argv(ffmpeg: str, path: Path, width: int, height: int,
                         fps: int, encoder: str = "h264_nvenc") -> list[str]:
    """Same encoder settings as the whole-session video pipe."""
    if encoder == "h264_nvenc":
        codec_args = ["-c:v", encoder, "-preset", "p4", "-tune", "hq",
                      "-rc", "vbr", "-cq", "20", "-b:v", "5M"]
    else:  # software fallback (tests, machines without NVENC)
        codec_args = ["-c:v", encoder, "-preset", "veryfast", "-crf", "23"]
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(fps),
        "-i",
        "-",
        "-an",
        *codec_args,
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]


class SessionClipRecorder:
    def __init__(self, clips_dir: Path, width: int, height: int, fps: int,
                 min_frames: int = 12, encoder: str = "h264_nvenc"):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is required for session clip capture")
        self._ffmpeg = ffmpeg
        self._encoder = encoder
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.fps = fps
        self.min_frames = min_frames
        self._index = 0
        self._active_id: str | None = None
        self._process: subprocess.Popen | None = None
        self._frames = 0
        self._path: Path | None = None

    @property
    def active_command_id(self) -> str | None:
        return self._active_id

    def poll(self, feed) -> None:
        """Open/close clips on command boundaries. Call once per control step
        (or per written frame); cheap when nothing changed."""
        current = feed.applied_command_id
        if self._active_id is not None and (
            current != self._active_id or self._active_id in feed.results
        ):
            self._close()
        if current is not None and self._active_id is None:
            self._open(current)

    def write_frame(self, frame: bytes) -> None:
        if self._process is None or self._process.stdin is None:
            return
        try:
            self._process.stdin.write(frame)
            self._frames += 1
        except (BrokenPipeError, ValueError):
            # encoder died; drop this clip, never take down the sim loop
            self._close()

    def _open(self, command_id: str) -> None:
        self._index += 1
        self._path = self.clips_dir / f"{self._index:04d}_{command_id[:8]}.mp4"
        self._process = subprocess.Popen(
            ffmpeg_rawvideo_argv(
                self._ffmpeg, self._path, self.width, self.height, self.fps,
                encoder=self._encoder),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._frames = 0
        self._active_id = command_id

    def _close(self) -> None:
        process, path, frames = self._process, self._path, self._frames
        self._process = None
        self._active_id = None
        self._frames = 0
        self._path = None
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=10)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        if frames < self.min_frames and path is not None:
            path.unlink(missing_ok=True)  # supersede churn, not a real action

    def close(self) -> None:
        self._close()
