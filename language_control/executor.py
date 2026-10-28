#!/usr/bin/env python3
"""Non-blocking executor built on subprocess.Popen.

State machine: idle -> starting -> running -> done/failed, with the cancel
path cancel_requested -> cancelled (entered only after the process group
is confirmed cleaned up). `stop` is handled locally by the CLI and calls
cancel() directly — it never goes through Kimi.

Three time concepts stay separate: sim_duration_s (playback steps x
control dt, informational), startup_timeout_s (marker must appear),
wall_timeout_s (resource guard only — it carries no motion-duration
semantics).

Results report three judgements separately: command constructed /
simulation process completed / motion-target evidence (without an
evaluator this is "unverified"; exit code 0 with a missing or mismatched
result file is never reported as success).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Callable

from .adapters import LaunchTemplate


STATES = (
    "idle",
    "starting",
    "running",
    "done",
    "failed",
    "cancel_requested",
    "cancelled",
)


class Executor:
    def __init__(
        self,
        on_event: Callable[[str, dict], None] | None = None,
        log_dir: Path | None = None,
    ):
        self.state = "idle"
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._task_id: str | None = None
        self._on_event = on_event or (lambda kind, fields: None)
        self._log_dir = log_dir
        self._monitor: threading.Thread | None = None
        self.result: dict | None = None

    # -- helpers ---------------------------------------------------------
    def _emit(self, kind: str, **fields) -> None:
        self._on_event(kind, fields)

    def _set_state(self, state: str) -> None:
        with self._lock:
            # A cancel in progress wins over any later transition attempt;
            # only the monitor's cleanup path may move it to cancelled.
            if self.state == "cancel_requested" and state != "cancelled":
                return
            self.state = state

    # -- submission ------------------------------------------------------
    def submit(self, task_id: str, template: LaunchTemplate, execute: bool) -> dict:
        with self._lock:
            if self.state not in ("idle", "done", "failed", "cancelled"):
                # Duplicate submission while a replay is active never starts
                # a second process.
                return {
                    "accepted": False,
                    "reason": f"executor is {self.state}; duplicate submission refused",
                    "task_id": task_id,
                }
            self.state = "starting" if execute else "idle"
            self._task_id = task_id
            self.result = None

        if not execute:
            result = {
                "accepted": True,
                "task_id": task_id,
                "dry_run": True,
                "command_constructed": True,
                "process_completed": None,
                "action_evidence": "unverified",
                "overall": "dry-run only: command constructed, nothing started",
                "state": "idle",
            }
            self.result = result
            self._emit(
                "execution",
                task_id=task_id,
                dry_run=True,
                command=template.public_dict(),
                verdict=result["overall"],
            )
            return result

        try:
            process = subprocess.Popen(
                template.argv,
                cwd=template.cwd,
                env=template.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,  # own process group for clean cancel
            )
        except OSError as exc:
            self._set_state("failed")
            result = {
                "accepted": True,
                "task_id": task_id,
                "dry_run": False,
                "command_constructed": True,
                "process_completed": False,
                "action_evidence": "unverified",
                "overall": f"failed to start: {exc}",
                "state": "failed",
            }
            self.result = result
            self._emit("execution", task_id=task_id, dry_run=False, error=str(exc))
            return result

        self._process = process
        self._emit(
            "execution",
            task_id=task_id,
            dry_run=False,
            pid=process.pid,
            command=template.public_dict(),
        )
        self._monitor = threading.Thread(
            target=self._monitor_process,
            args=(task_id, template, process),
            daemon=True,
        )
        self._monitor.start()
        return {"accepted": True, "task_id": task_id, "pid": process.pid, "state": "starting"}

    # -- monitoring ------------------------------------------------------
    def _monitor_process(
        self, task_id: str, template: LaunchTemplate, process: subprocess.Popen
    ) -> None:
        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()

        log_file = None
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_file = (self._log_dir / f"{task_id}.log").open("w", encoding="utf-8")

        started = time.monotonic()
        marker_seen = template.startup_marker is None
        marker_deadline = started + template.startup_timeout_s
        wall_deadline = started + template.wall_timeout_s
        captured: list[str] = []
        reader_done = False
        cancel_seen = False
        timeout_error: str | None = None

        while True:
            if self.state == "cancel_requested" and not cancel_seen:
                cancel_seen = True
                self._kill_group(process)
            if not marker_seen and time.monotonic() > marker_deadline and not cancel_seen:
                timeout_error = f"startup timeout ({template.startup_timeout_s:.0f}s)"
                self._kill_group(process)
                cancel_seen = True  # reuse the cleanup path
            if time.monotonic() > wall_deadline and not cancel_seen:
                timeout_error = f"wall timeout ({template.wall_timeout_s:.0f}s)"
                self._kill_group(process)
                cancel_seen = True
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                line = ""
            if line is None:
                reader_done = True
            elif line:
                captured.append(line.rstrip("\n"))
                if log_file is not None:
                    log_file.write(line)
                    log_file.flush()
                if not marker_seen and template.startup_marker in line:
                    marker_seen = True
                    self._set_state("running")
                    self._emit("execution", task_id=task_id, stage="running", pid=process.pid)
            if reader_done and process.poll() is not None:
                break
            if cancel_seen and process.poll() is not None and reader_done:
                break

        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if log_file is not None:
            log_file.close()

        if self.state == "cancel_requested":
            # cancelled only after the process group is confirmed gone.
            self._set_state("cancelled")
            self.result = {
                "accepted": True,
                "task_id": task_id,
                "dry_run": False,
                "command_constructed": True,
                "process_completed": False,
                "action_evidence": "unverified",
                "overall": "cancelled: process group cleaned up",
                "state": "cancelled",
            }
            self._emit("cancel", task_id=task_id, stage="cancelled", pid=process.pid)
            return

        self._finalize(
            task_id, template, process.returncode, captured, marker_seen, timeout_error
        )

    def _finalize(self, task_id, template, exit_code, captured, marker_seen, error):
        process_completed = error is None and marker_seen and exit_code == 0
        evidence = "unverified"
        if process_completed:
            evidence = self._check_evidence(template)
        state = "done" if process_completed and evidence == "passed" else "failed"
        if error is None and exit_code == 0 and evidence != "passed":
            state = "failed"
            error = f"process exited 0 but result evidence is {evidence}; not a success"
        elif error is None and exit_code != 0:
            error = f"process exit code {exit_code}"
        overall = (
            "completed one independent replay with verifiable result"
            if state == "done"
            else f"failed: {error}"
        )
        self._set_state(state)
        self.result = {
            "accepted": True,
            "task_id": task_id,
            "dry_run": False,
            "command_constructed": True,
            "process_completed": process_completed,
            "action_evidence": evidence,
            "exit_code": exit_code,
            "overall": overall,
            "state": state,
            "log_tail": captured[-20:],
        }
        self._emit(
            "execution",
            task_id=task_id,
            stage=state,
            exit_code=exit_code,
            process_completed=process_completed,
            action_evidence=evidence,
            overall=overall,
        )

    def _check_evidence(self, template: LaunchTemplate) -> str:
        """Match the result file against expectations; never infer success
        from the exit code alone."""
        if template.output_file is None:
            return "unverified"
        output = Path(template.output_file)
        if not output.is_file() or output.stat().st_size == 0:
            return "missing"
        try:
            summary = json.loads(output.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "mismatch"
        expected = template.expected
        if "steps" in expected and summary.get("steps") != expected["steps"]:
            return "mismatch"
        if "checkpoint" in expected and summary.get("checkpoint") != expected["checkpoint"]:
            return "mismatch"
        if "policy" in expected and summary.get("policy") != expected["policy"]:
            return "mismatch"
        if "command" in expected:
            actual = summary.get("command")
            if actual is None or [float(v) for v in actual] != [
                float(v) for v in expected["command"]
            ]:
                return "mismatch"
        if expected.get("encoding") == "phase":
            trace = Path(template.trace_file) if template.trace_file else None
            if trace is None or not trace.is_file() or trace.stat().st_size == 0:
                return "missing"
            try:
                rows = json.loads(trace.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return "mismatch"
            if not rows or rows[0].get("dance_phase") is None:
                return "mismatch"
            if len(rows) != expected["steps"]:
                return "mismatch"
        return "passed"

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        for sig, grace in (
            (signal.SIGINT, 3.0),
            (signal.SIGTERM, 3.0),
            (signal.SIGKILL, 5.0),
        ):
            if process.poll() is not None:
                return
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(0.05)

    # -- cancel ----------------------------------------------------------
    def cancel(self, reason: str = "stop requested") -> dict:
        with self._lock:
            if self.state not in ("starting", "running"):
                return {
                    "cancelled": False,
                    "reason": f"nothing to cancel (state={self.state})",
                    "state": self.state,
                }
            self.state = "cancel_requested"
            task_id = self._task_id
        self._emit("cancel", task_id=task_id, stage="cancel_requested", reason=reason)
        if self._monitor is not None:
            self._monitor.join(timeout=30.0)
        return {"cancelled": self.state == "cancelled", "state": self.state}
