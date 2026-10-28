#!/usr/bin/env python3
"""Session-level command/ACK protocol for persistent playback (M1-A).

Isaac-free module: the play script (scripts/play_isaac_velocity.py) and the
test double (scripts/sim_stub_playback.py) keep only thin calls into this
file, so the whole protocol state machine is unit-testable without Isaac.

Protocol overview (task book §3.1):
- One writer (the session control service) atomically replaces the command
  file (write tmp + os.replace). This module is the reader side.
- A command carries protocol_version / session_id+session_epoch /
  command_id / sequence / skill_id / contract_version / kind / encoding /
  mutually exclusive typed params / coordinate_frame / units /
  sim_duration_s / lease_s / issued_at.
- Reject: NaN/Inf, unknown fields, encoding conflicts, invalid types,
  unknown kinds, wrong session/epoch, stale or out-of-order sequences.
- Dedup: re-reading the same command_id+sequence is a no-op; re-sending the
  same command_id with a higher sequence is a lease renewal — it never
  restarts a finite command or resets its completion timer.
- Lease (sim time): a streaming setpoint expires lease_s of sim time after
  its last application/renewal; expiry enters an explicit fault-stop state
  instead of holding the old velocity forever.
- Watchdog (wall clock): after the first valid command, a stale command-file
  mtime means the writer exited or stalled -> fault-stop.
- Finite commands (sim_duration_s) complete by sim time measured from the
  applied ACK, then the velocity setpoint falls back to zero.
- Episode reset interrupts the active command explicitly
  (interrupted_by_reset) — never a silent continuation.

ACK file (reader -> writer), rewritten atomically on change:
  {protocol_version, session_id, session_epoch,
   acks: [{command_id, sequence, status: applied|rejected, reason,
           applied_step, sim_time_s, renewal}],  # bounded tail
   results: {command_id: {status: completed|interrupted_by_reset|stopped,
                          applied_step, applied_sim_time_s, finished_step,
                          finished_sim_time_s, sim_steps_executed, ...}},
   updated_step, sim_time_s}

Status snapshot (writer: play loop, every control step, atomic): see
write_status(). Consumers must check wall_updated_at freshness; a stale
snapshot is never evidence of a fresh stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import time
from typing import Any


PROTOCOL_VERSION = 1

COMMAND_KINDS = (
    "velocity_setpoint",
    "stop_skill",
    "pause_sim",
    "resume_sim",
    "cancel_session",
)
CONTROL_KINDS = ("stop_skill", "pause_sim", "resume_sim", "cancel_session")
ENCODINGS = ("velocity", "phase", "posture_flag", "constant")
COORDINATE_FRAMES = ("base", "world", "initial_heading")
VELOCITY_FIELDS = ("vx", "vy", "yaw")
SUPPORTED_UNITS = {"linear": "m/s", "angular": "rad/s"}

COMMAND_FIELDS = frozenset({
    "protocol_version",
    "session_id",
    "session_epoch",
    "command_id",
    "sequence",
    "skill_id",
    "contract_version",
    "kind",
    "encoding",
    "params",
    "coordinate_frame",
    "units",
    "sim_duration_s",
    "lease_s",
    "issued_at",
})

MAX_ACKS_KEPT = 200

RESULT_COMPLETED = "completed"
RESULT_INTERRUPTED = "interrupted_by_reset"
RESULT_STOPPED = "stopped"


class ProtocolError(Exception):
    """A command failed structural or session validation (ACK: rejected)."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _check_finite_tree(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _check_finite_tree(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_finite_tree(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError(f"{path} is NaN/Infinity; rejected")


def _loads_strict(text: str) -> dict:
    def _reject_constant(name: str) -> None:
        raise ProtocolError(f"NaN/Infinity literal {name!r} in command file; rejected")

    try:
        payload = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"command file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("command file must contain a JSON object")
    return payload


@dataclass
class Command:
    session_id: str
    session_epoch: int
    command_id: str
    sequence: int
    skill_id: str
    contract_version: str
    kind: str
    encoding: str
    params: dict
    coordinate_frame: str
    units: dict
    sim_duration_s: float | None
    lease_s: float | None
    issued_at: str

    @classmethod
    def from_dict(cls, raw: dict) -> "Command":
        unknown = set(raw) - COMMAND_FIELDS
        if unknown:
            raise ProtocolError(f"unknown command fields: {sorted(unknown)}")
        missing = COMMAND_FIELDS - set(raw)
        if missing:
            raise ProtocolError(f"missing command fields: {sorted(missing)}")
        if raw["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError(
                f"protocol_version {raw['protocol_version']!r} != {PROTOCOL_VERSION}"
            )
        for name in ("session_id", "command_id", "skill_id", "contract_version", "issued_at"):
            if not isinstance(raw[name], str) or not raw[name]:
                raise ProtocolError(f"{name} must be a non-empty string")
        if not _is_int(raw["session_epoch"]) or raw["session_epoch"] < 1:
            raise ProtocolError("session_epoch must be a positive integer")
        if not _is_int(raw["sequence"]) or raw["sequence"] < 1:
            raise ProtocolError("sequence must be a positive integer")
        kind = raw["kind"]
        if kind not in COMMAND_KINDS:
            raise ProtocolError(f"unknown command kind {kind!r}")
        encoding = raw["encoding"]
        if encoding not in ENCODINGS:
            raise ProtocolError(f"unknown command encoding {encoding!r}")
        params = raw["params"]
        if not isinstance(params, dict):
            raise ProtocolError("params must be an object")
        _check_finite_tree(params, "params")
        if kind == "velocity_setpoint":
            # typed parameters are mutually exclusive and must match the
            # declared encoding exactly once
            if set(params) != {encoding}:
                raise ProtocolError(
                    f"encoding conflict: encoding={encoding!r} but params keys "
                    f"{sorted(params)}; typed parameter blocks are mutually exclusive"
                )
            block = params[encoding]
            if not isinstance(block, dict):
                raise ProtocolError(f"params.{encoding} must be an object")
            if encoding == "velocity":
                if set(block) - set(VELOCITY_FIELDS):
                    raise ProtocolError(
                        f"unknown velocity fields {sorted(set(block) - set(VELOCITY_FIELDS))}"
                    )
                for name in VELOCITY_FIELDS:
                    if name in block and not _is_finite_number(block[name]):
                        raise ProtocolError(f"velocity.{name} must be a finite number")
        else:  # control kinds carry no typed parameters
            if params:
                raise ProtocolError(f"{kind} takes no typed params")
        if raw["coordinate_frame"] not in COORDINATE_FRAMES:
            raise ProtocolError(
                f"unknown coordinate_frame {raw['coordinate_frame']!r}"
            )
        units = raw["units"]
        if not isinstance(units, dict) or units != SUPPORTED_UNITS:
            raise ProtocolError(
                f"unsupported units {units!r}; only {SUPPORTED_UNITS} are implemented"
            )
        sim_duration_s = raw["sim_duration_s"]
        if sim_duration_s is not None and not (
            _is_finite_number(sim_duration_s) and sim_duration_s > 0
        ):
            raise ProtocolError("sim_duration_s must be null or a positive finite number")
        lease_s = raw["lease_s"]
        if lease_s is not None and not (_is_finite_number(lease_s) and lease_s > 0):
            raise ProtocolError("lease_s must be null or a positive finite number")
        if kind == "velocity_setpoint" and lease_s is None:
            raise ProtocolError("velocity_setpoint requires a finite lease_s")
        return cls(
            session_id=raw["session_id"],
            session_epoch=raw["session_epoch"],
            command_id=raw["command_id"],
            sequence=raw["sequence"],
            skill_id=raw["skill_id"],
            contract_version=raw["contract_version"],
            kind=kind,
            encoding=encoding,
            params=params,
            coordinate_frame=raw["coordinate_frame"],
            units=units,
            sim_duration_s=sim_duration_s,
            lease_s=lease_s,
            issued_at=raw["issued_at"],
        )

    def velocity(self) -> tuple[float, float, float]:
        block = self.params.get("velocity", {})
        return (
            float(block.get("vx", 0.0)),
            float(block.get("vy", 0.0)),
            float(block.get("yaw", 0.0)),
        )

    def same_payload_as(self, other: "Command") -> bool:
        return (
            self.kind == other.kind
            and self.encoding == other.encoding
            and self.params == other.params
            and self.skill_id == other.skill_id
            and self.contract_version == other.contract_version
        )


def atomic_write_json(path: Path, payload: dict) -> None:
    """Single-writer atomic replace: tmp file in the same directory + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_status(path: Path, snapshot: dict) -> None:
    """Atomically publish the bounded real-time status snapshot."""
    atomic_write_json(path, snapshot)


def read_json(path: Path) -> dict | None:
    """Best-effort read of a protocol JSON file; None when missing/corrupt."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class _Active:
    command: Command
    applied_step: int
    applied_sim_time_s: float
    lease_deadline_sim_s: float


class CommandFeed:
    """Play-side reader state machine for one simulation session."""

    def __init__(
        self,
        command_path: Path,
        session_id: str,
        session_epoch: int,
        control_dt_s: float,
        ack_path: Path | None = None,
        watchdog_wall_s: float = 5.0,
    ):
        self.command_path = Path(command_path)
        self.session_id = session_id
        self.session_epoch = session_epoch
        self.control_dt_s = control_dt_s
        self.ack_path = Path(ack_path) if ack_path is not None else None
        self.watchdog_wall_s = watchdog_wall_s

        self.last_sequence = 0
        self._seen: dict[str, int] = {}  # command_id -> highest sequence seen
        self._active: _Active | None = None
        self.paused = False
        self.cancel_requested = False
        self.fault: str | None = None
        self.acks: list[dict] = []
        self.results: dict[str, dict] = {}
        self.episode_id = 0
        self.reset_reason: str | None = None
        self._last_reset_step: int | None = None
        self._armed = False  # watchdog arms after the command file first exists
        self._acks_dirty = False
        self._last_error: str | None = None

    # -- ACK/result recording ---------------------------------------------
    def _ack(
        self,
        command: Command,
        status: str,
        sim_step: int,
        sim_time_s: float,
        reason: str = "",
        renewal: bool = False,
    ) -> None:
        self.acks.append({
            "command_id": command.command_id,
            "sequence": command.sequence,
            "skill_id": command.skill_id,
            "kind": command.kind,
            "status": status,
            "reason": reason,
            "applied_step": sim_step,
            "sim_time_s": sim_time_s,
            "renewal": renewal,
        })
        del self.acks[:-MAX_ACKS_KEPT]
        self._acks_dirty = True

    def _finish_active(
        self, status: str, sim_step: int, sim_time_s: float, reason: str = ""
    ) -> None:
        active = self._active
        if active is None:
            return
        self.results[active.command.command_id] = {
            "status": status,
            "reason": reason,
            "command_id": active.command.command_id,
            "skill_id": active.command.skill_id,
            "applied_step": active.applied_step,
            "applied_sim_time_s": active.applied_sim_time_s,
            "finished_step": sim_step,
            "finished_sim_time_s": sim_time_s,
            "sim_steps_executed": sim_step - active.applied_step,
            "episode_id": self.episode_id,
        }
        self._active = None
        self._acks_dirty = True

    def _enter_fault(self, reason: str, sim_step: int, sim_time_s: float) -> None:
        if self.fault is None:
            self.fault = reason
        # an active command never survives a fault: the old setpoint is
        # dropped, the robot gets zero velocity, motion needs a new request
        self._finish_active(RESULT_STOPPED, sim_step, sim_time_s, reason=f"fault: {reason}")

    # -- public state ------------------------------------------------------
    def current_velocity(self) -> tuple[float, float, float]:
        """Effective velocity setpoint for this control step.

        Zero unless a live, leased, unfaulted velocity command is active.
        """
        if self.fault is not None or self.paused or self.cancel_requested:
            return (0.0, 0.0, 0.0)
        if self._active is None or self._active.command.kind != "velocity_setpoint":
            return (0.0, 0.0, 0.0)
        return self._active.command.velocity()

    @property
    def applied_command_id(self) -> str | None:
        return None if self._active is None else self._active.command.command_id

    @property
    def run_state(self) -> str:
        if self.cancel_requested:
            return "cancelled"
        if self.fault is not None:
            return "fault_stopped"
        if self.paused:
            return "paused"
        return "running"

    # -- episode reset ------------------------------------------------------
    def on_episode_reset(
        self, episode_id: int, reason: str, sim_step: int, sim_time_s: float
    ) -> None:
        """Record an env reset: the active command is explicitly interrupted."""
        self.episode_id = episode_id
        self.reset_reason = reason
        self._last_reset_step = sim_step
        self._finish_active(
            RESULT_INTERRUPTED, sim_step, sim_time_s, reason=f"episode reset: {reason}"
        )
        self._flush_acks(sim_step, sim_time_s)

    # -- main poll -----------------------------------------------------------
    def poll(self, sim_step: int, sim_time_s: float | None = None) -> None:
        """Read the command file once per control step and update state."""
        if sim_time_s is None:
            sim_time_s = sim_step * self.control_dt_s
        if self.cancel_requested:
            return

        # watchdog on real wall-clock freshness of the command file: a stale
        # or vanished file after arming means the writer exited or stalled.
        # Atomic replace never exposes a missing destination, so a missing
        # file here is a real loss, not a rename window.
        try:
            mtime: float | None = self.command_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None:
            self._armed = True
        if self._armed:
            if mtime is None:
                self._enter_fault("command_file_missing", sim_step, sim_time_s)
            elif time.time() - mtime > self.watchdog_wall_s:
                self._enter_fault("writer_watchdog_timeout", sim_step, sim_time_s)

        # lease expiry in sim time
        active = self._active
        if active is not None and sim_time_s > active.lease_deadline_sim_s:
            self._enter_fault("lease_expired", sim_step, sim_time_s)

        # finite command completion in control steps (sim time via float
        # subtraction is off-by-one prone: e.g. 873*0.02-853*0.02 < 0.4)
        active = self._active
        if (
            active is not None
            and active.command.sim_duration_s is not None
            and sim_step - active.applied_step
            >= round(active.command.sim_duration_s / self.control_dt_s)
        ):
            self._finish_active(RESULT_COMPLETED, sim_step, sim_time_s)

        raw_text: str | None = None
        try:
            raw_text = self.command_path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, PermissionError, UnicodeDecodeError):
            raw_text = None
        if raw_text is None:
            self._flush_acks(sim_step, sim_time_s)
            return
        try:
            payload = _loads_strict(raw_text)
            command = Command.from_dict(payload)
        except ProtocolError as exc:
            # corrupt/invalid file: no new command; the lease and watchdog
            # bound how long any previous setpoint can survive
            self._last_error = str(exc)
            self._flush_acks(sim_step, sim_time_s)
            return

        if (
            command.session_id != self.session_id
            or command.session_epoch != self.session_epoch
        ):
            # a different session's command is never applied; reject it so the
            # writer learns, but do not fault this session
            self._reject(command, "session_id/epoch mismatch", sim_step, sim_time_s)
            self._flush_acks(sim_step, sim_time_s)
            return

        seen_sequence = self._seen.get(command.command_id)
        if seen_sequence is not None and command.sequence <= seen_sequence:
            # identical re-read or stale retransmission: idempotent no-op
            self._flush_acks(sim_step, sim_time_s)
            return
        if command.sequence <= self.last_sequence and command.command_id not in self._seen:
            self._reject(command, "out-of-order or expired sequence", sim_step, sim_time_s)
            self._flush_acks(sim_step, sim_time_s)
            return

        is_renewal = (
            self._active is not None
            and command.command_id == self._active.command.command_id
        )
        if is_renewal and not command.same_payload_as(self._active.command):
            self._reject(
                command,
                "renewal with changed payload; use a new command_id",
                sim_step,
                sim_time_s,
            )
            self._flush_acks(sim_step, sim_time_s)
            return

        self._seen[command.command_id] = command.sequence
        self.last_sequence = command.sequence

        if is_renewal:
            # lease renewal: extends the lease, never restarts the command or
            # resets its completion timer
            self._active.lease_deadline_sim_s = sim_time_s + (command.lease_s or 0.0)
            self._ack(command, "applied", sim_step, sim_time_s, renewal=True)
        elif command.kind == "velocity_setpoint":
            if command.encoding != "velocity":
                self._reject(
                    command,
                    f"encoding {command.encoding!r} not supported by this runtime session",
                    sim_step,
                    sim_time_s,
                )
            else:
                self._finish_active(RESULT_STOPPED, sim_step, sim_time_s, reason="superseded")
                self._active = _Active(
                    command=command,
                    applied_step=sim_step,
                    applied_sim_time_s=sim_time_s,
                    lease_deadline_sim_s=sim_time_s + command.lease_s,
                )
                self.fault = None  # a new valid request clears the fault-stop state
                self._ack(command, "applied", sim_step, sim_time_s)
        elif command.kind == "stop_skill":
            # velocity-family stop: the skill's verified exit is a zero
            # setpoint; simulation keeps running
            self._finish_active(RESULT_STOPPED, sim_step, sim_time_s, reason="stop_skill")
            self._ack(command, "applied", sim_step, sim_time_s)
        elif command.kind == "pause_sim":
            self.paused = True
            self._ack(command, "applied", sim_step, sim_time_s)
        elif command.kind == "resume_sim":
            self.paused = False
            self._ack(command, "applied", sim_step, sim_time_s)
        elif command.kind == "cancel_session":
            self._finish_active(RESULT_STOPPED, sim_step, sim_time_s, reason="cancel_session")
            self.cancel_requested = True
            self._ack(command, "applied", sim_step, sim_time_s)
        self._flush_acks(sim_step, sim_time_s)

    def _reject(self, command: Command, reason: str, sim_step: int, sim_time_s: float) -> None:
        self._ack(command, "rejected", sim_step, sim_time_s, reason=reason)

    def _flush_acks(self, sim_step: int, sim_time_s: float) -> None:
        if self.ack_path is None or not self._acks_dirty:
            return
        atomic_write_json(self.ack_path, {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "acks": list(self.acks),
            "results": dict(self.results),
            "updated_step": sim_step,
            "sim_time_s": sim_time_s,
        })
        self._acks_dirty = False
