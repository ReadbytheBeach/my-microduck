#!/usr/bin/env python3
"""Single-session control service (M1-A/M1-B).

One SessionService process owns the exclusive write permission of one
simulation session's command file. mdctl (and later Web) are clients: they
drop request files into requests/ and read responses/; they never write the
command file themselves. The service:

- spawns the playback process (isaac backend -> scripts/play_isaac_velocity.py
  session mode; stub backend -> scripts/sim_stub_playback.py test double);
- assigns session_id/epoch, command_id and sequence numbers;
- gates every step through the same Gateway/registry as the M0 chain
  (limits, usage/evidence states; cross-model skills refused until M1-C;
  stand maps to a zero-velocity param switch on the session policy);
- runs sequences step-by-step: the next step is sent only after the previous
  step's applied ACK and result; a rejected/failed/interrupted step aborts
  the rest of the sequence — no overwriting, no lost steps;
- maintains leases (renewal) and the writer heartbeat; the play-side
  watchdog/lease logic lives in scripts/playback_command_feed.py;
- implements the stop three-semantics: stop_skill (velocity zero command,
  sim keeps running), pause_sim/resume_sim, cancel_session (process ends,
  evidence kept). Stop never waits on an LLM and invalidates queued work,
  including requests created before the stop that arrive late.

Runs as a daemon: python -m language_control.session_service --session-dir ...
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playback_command_feed as pcf  # noqa: E402

from .adapters import ISAAC_KIT_ARGS, ISAACLAB_SH, minimal_env  # noqa: E402
from . import closed_loop  # noqa: E402
from .gateway import Gateway  # noqa: E402
from .ledger_log import Ledger  # noqa: E402
from .proposal import ProposalError, Step, new_proposal  # noqa: E402


DEFAULT_REGISTRY = PROJECT_ROOT / "work/language_control/skill_registry.json"
DEFAULT_LEDGER = PROJECT_ROOT / "work/language_control/events.jsonl"
DEFAULT_SESSIONS_ROOT = PROJECT_ROOT / "work/language_control/sessions"

LOOP_PERIOD_S = 0.05
HEARTBEAT_PERIOD_S = 1.0
STREAMING_LEASE_S = 3.0
FINITE_LEASE_MARGIN_S = 2.0
STATUS_STALE_S = 3.0
CANCEL_GRACE_S = 10.0
TERMINAL_STATES = ("completed", "failed", "rejected", "stopped", "interrupted", "cancelled")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionPaths:
    def __init__(self, session_dir: Path):
        self.dir = Path(session_dir)
        self.meta = self.dir / "session.json"
        self.requests = self.dir / "requests"
        self.responses = self.dir / "responses"
        self.command = self.dir / "command.json"
        self.ack = self.dir / "ack.json"
        self.status = self.dir / "status.json"
        self.play_log = self.dir / "play.log"
        self.play_output = self.dir / "playback.json"


class SessionService:
    def __init__(
        self,
        session_dir: Path,
        registry_path: Path = DEFAULT_REGISTRY,
        ledger_path: Path = DEFAULT_LEDGER,
        backend: str = "stub",
        name: str = "sim01",
        scene: str = "flat",
        steps: int = 1500,
        seed: int = 42,
        stub_options: dict | None = None,
        step_wall_timeout_factor: float = 4.0,
        streaming_lease_s: float = STREAMING_LEASE_S,
    ):
        if backend not in ("isaac", "stub"):
            raise ValueError(f"unknown backend {backend!r}")
        self.paths = SessionPaths(session_dir)
        self.registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        self.ledger = Ledger(ledger_path)
        self.gateway = Gateway(self.registry)
        self.backend = backend
        self.name = name
        self.scene = scene
        self.steps = steps
        self.seed = seed
        self.stub_options = dict(stub_options or {})
        self.step_wall_timeout_factor = step_wall_timeout_factor
        self.streaming_lease_s = streaming_lease_s

        self.session_id = uuid.uuid4().hex
        self.session_epoch = 1
        # M1 sessions bind exactly one policy: the velocity walk contract
        walk = self.registry["skills"]["walk"]
        self.policy_model = walk["model"]["path"]
        self.policy_sha256 = walk["model"]["actual_sha256"]
        self.contract_version = f"sha256:{self.policy_sha256}"

        self.state = "starting"
        self._started_wall = time.time()  # requests older than this are stale
        self._sequence = 0
        self._process: subprocess.Popen | None = None
        self._requests: dict[str, dict] = {}
        self._processed_request_files: set[str] = set()
        self._active: dict | None = None  # active command tracking
        self._queue: list[dict] = []  # pending sequence steps
        self._last_stop_wall = 0.0
        self._last_write_wall = 0.0
        self._status_seen = False
        self._shutdown = False

    # -- command construction ----------------------------------------------
    def _resolve_step(self, skill: str, params: dict) -> dict:
        """Map a proposal step to a velocity command on the session policy.

        Returns {"velocity": {...}, "sim_duration_s": ...} or raises
        ProposalError. Only velocity-encoding skills on the session policy
        are executable in M1; stand is the verified same-policy zero switch.
        """
        if skill == "stop":
            return {"control": "stop_skill"}
        record = self.registry["skills"].get(skill)
        if record is None:
            raise ProposalError("capability_gap", f"skill {skill!r} is not in the registry")
        encoding = record.get("command", {}).get("encoding")
        if encoding != "velocity":
            raise ProposalError(
                "capability_gap",
                f"{skill} uses {encoding!r} encoding; persistent M1 sessions execute "
                "velocity-encoding skills only (其余编码待 M1-C/M2 验证)",
            )
        same_model = record.get("model", {}).get("path") == self.policy_model
        # §3.6 closed-loop goals (sim_ground_truth feedback) — velocity family
        if "distance_m" in params or "turn_deg" in params:
            if "distance_m" in params and "turn_deg" in params:
                raise ProposalError("policy_rejected", "distance_m and turn_deg are mutually exclusive")
            if not same_model:
                raise ProposalError(
                    "policy_rejected", "closed-loop goals run on the session policy only")
            if any(k in params for k in ("vx", "vy", "yaw")):
                raise ProposalError(
                    "policy_rejected", "closed-loop goals take no raw velocity params")
            # cruise/turn rates are inside the registry velocity limits; verify once
            gate = self.gateway.check(
                new_proposal(steps=[Step(skill="walk", params={"vx": closed_loop.CRUISE_VX})],
                             raw_text="session-step:closed-loop", source="session"),
                execute=True)
            if not gate.allowed:
                raise ProposalError(gate.classification, gate.reason)
            try:
                if "distance_m" in params:
                    controller = closed_loop.DistanceController(float(params["distance_m"]))
                    return {"closed_loop": "distance", "controller": controller}
                controller = closed_loop.TurnController(float(params["turn_deg"]))
                return {"closed_loop": "turn", "controller": controller}
            except ValueError as exc:
                raise ProposalError(
                    "capability_gap",
                    f"{exc}; outside the closed-loop range (估算模式不静默替换闭环要求)")
        if not same_model:
            if skill == "stand":
                # walk/stand share velstand: same-policy zero-velocity switch,
                # NOT a cross-model transition (task book §3.5 correction)
                velocity = {"vx": 0.0, "vy": 0.0, "yaw": 0.0}
            else:
                raise ProposalError(
                    "policy_rejected",
                    f"{skill} binds a different model; cross-model switching is "
                    "M1-C and stays unverified/refused",
                )
        else:
            velocity = {
                "vx": float(params.get("vx", 0.0)),
                "vy": float(params.get("vy", 0.0)),
                "yaw": float(params.get("yaw", 0.0)),
            }
        # per-encoding limits through the same gateway as M0
        check_params = {k: v for k, v in velocity.items() if v != 0.0 or k in params}
        proposal = new_proposal(
            steps=[Step(skill="walk", params=check_params)],
            raw_text=f"session-step:{skill}",
            source="session",
        )
        gate = self.gateway.check(proposal, execute=True)
        if not gate.allowed:
            raise ProposalError(gate.classification, gate.reason)
        duration = params.get("sim_duration_s")
        if duration is not None:
            duration = float(duration)
            if duration <= 0:
                raise ProposalError("parse_error", "sim_duration_s must be positive")
        elif not same_model:
            # stand-as-zero-switch defaults to a bounded 2 s hold so that
            # sequences reach a terminal state instead of holding forever
            duration = 2.0
        return {
            "velocity": velocity,
            "sim_duration_s": duration,
            "same_policy_switch": not same_model,
        }

    def _build_command(self, resolved: dict) -> dict:
        self._sequence += 1
        duration = resolved.get("sim_duration_s")
        if "control" in resolved:
            kind = resolved["control"]
            params: dict = {}
            lease_s = None
            sim_duration_s = None
        else:
            kind = "velocity_setpoint"
            params = {"velocity": resolved["velocity"]}
            sim_duration_s = duration
            lease_s = (
                self.streaming_lease_s
                if duration is None
                else duration + FINITE_LEASE_MARGIN_S
            )
        return {
            "protocol_version": pcf.PROTOCOL_VERSION,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "command_id": uuid.uuid4().hex,
            "sequence": self._sequence,
            "skill_id": resolved.get("skill", "session"),
            "contract_version": self.contract_version,
            "kind": kind,
            "encoding": "velocity",
            "params": params,
            "coordinate_frame": "base",
            "units": dict(pcf.SUPPORTED_UNITS),
            "sim_duration_s": sim_duration_s,
            "lease_s": lease_s,
            "issued_at": _now_iso(),
        }

    def _write_command(self, command: dict) -> None:
        pcf.atomic_write_json(self.paths.command, command)
        self._last_write_wall = time.monotonic()
        self.ledger.append(
            "command",
            session_id=self.session_id,
            command_id=command["command_id"],
            sequence=command["sequence"],
            skill_id=command["skill_id"],
            kind=command["kind"],
            params=command["params"],
            sim_duration_s=command["sim_duration_s"],
            lease_s=command["lease_s"],
        )

    # -- playback process ----------------------------------------------------
    def _play_argv(self) -> tuple[list[str], dict | None]:
        paths = self.paths
        if self.backend == "stub":
            argv = [
                sys.executable,
                str(_SCRIPTS / "sim_stub_playback.py"),
                "--command-file", str(paths.command),
                "--status-file", str(paths.status),
                "--ack-file", str(paths.ack),
                "--session-id", self.session_id,
                "--session-epoch", str(self.session_epoch),
                "--steps", str(self.steps),
                "--policy-sha256", self.policy_sha256,
                "--output", str(paths.play_output),
            ]
            for key, flag in (
                ("control_dt", "--control-dt"),
                ("tick_sleep", "--tick-sleep"),
                ("episode_steps", "--episode-steps"),
                ("reject_vx", "--reject-vx"),
                ("watchdog_s", "--watchdog-s"),
                ("freeze", "--freeze"),
            ):
                if key in self.stub_options:
                    value = self.stub_options[key]
                    argv += [flag, str(int(value) if key == "episode_steps" else value)]
            return argv, None
        argv = [
            str(ISAACLAB_SH),
            "-p",
            str(_SCRIPTS / "play_isaac_velocity.py"),
            "--headless",
            "--device", "cuda:0",
            "--policy-onnx", self.policy_model,
            "--num-envs", "1",
            "--seed", str(self.seed),
            "--steps", str(self.steps),
            "--command-file", str(paths.command),
            "--status-file", str(paths.status),
            "--ack-file", str(paths.ack),
            "--session-id", self.session_id,
            "--session-epoch", str(self.session_epoch),
            "--output", str(paths.play_output),
            "--kit_args", ISAAC_KIT_ARGS,
        ]
        env = minimal_env()
        if os.environ.get("MICRODUCK_DEBUG_OBS"):  # debug passthrough (off by default)
            env["MICRODUCK_DEBUG_OBS"] = "1"
        return argv, env

    def start_play(self) -> None:
        for directory in (self.paths.dir, self.paths.requests, self.paths.responses):
            directory.mkdir(parents=True, exist_ok=True)
        argv, env = self._play_argv()
        log = self.paths.play_log.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            argv,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.ledger.append(
            "session",
            event="start",
            session_id=self.session_id,
            session_epoch=self.session_epoch,
            backend=self.backend,
            name=self.name,
            scene=self.scene,
            pid=self._process.pid,
            policy_sha256=self.policy_sha256,
            steps=self.steps,
        )
        self._write_meta()

    def _write_meta(self) -> None:
        pcf.atomic_write_json(self.paths.meta, {
            "name": self.name,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "backend": self.backend,
            "scene": self.scene,
            "state": self.state,
            "policy_model": self.policy_model,
            "policy_sha256": self.policy_sha256,
            "contract_version": self.contract_version,
            "pid_service": os.getpid(),
            "pid_play": None if self._process is None else self._process.pid,
            "created_at": _now_iso(),
            "steps": self.steps,
        })

    # -- request intake --------------------------------------------------------
    def _scan_requests(self) -> list[dict]:
        try:
            files = sorted(self.paths.requests.glob("*.json"))
        except FileNotFoundError:
            return []
        out = []
        for path in files:
            if path.name in self._processed_request_files:
                continue
            payload = pcf.read_json(path)
            self._processed_request_files.add(path.name)
            if payload is None:
                continue
            out.append(payload)
        return out

    def _write_response(self, request_id: str, response: dict) -> None:
        response = {"request_id": request_id, "updated_at": _now_iso(), **response}
        pcf.atomic_write_json(self.paths.responses / f"{request_id}.json", response)
        self._requests.setdefault(request_id, {})["response"] = response

    # -- sequence engine -------------------------------------------------------
    def _submit_steps(self, request: dict) -> None:
        """Validate all steps statically, then enqueue for stepwise execution."""
        request_id = request["request_id"]
        steps = request.get("steps", [])
        try:
            proposal = new_proposal(
                steps=[Step(skill=s["skill"], params=s.get("params", {})) for s in steps],
                raw_text=request.get("raw_text", "session-request"),
                source=request.get("source", "cli"),
                max_steps=8,
            )
        except ProposalError as exc:
            self._write_response(request_id, {
                "status": "rejected", "classification": exc.classification,
                "reason": exc.reason,
            })
            return
        resolved = []
        for index, step in enumerate(proposal.steps):
            try:
                item = self._resolve_step(step.skill, step.params)
            except ProposalError as exc:
                self.ledger.append(
                    "sequence", request_id=request_id, event="static_reject",
                    step_index=index, skill=step.skill,
                    classification=exc.classification, reason=exc.reason,
                )
                self._write_response(request_id, {
                    "status": "rejected", "classification": exc.classification,
                    "reason": f"step {index} ({step.skill}): {exc.reason}",
                })
                return
            item["skill"] = step.skill
            item["request_id"] = request_id
            item["step_index"] = index
            resolved.append(item)
        if self.state == "paused":
            self._write_response(request_id, {
                "status": "rejected", "classification": "policy_rejected",
                "reason": "session is paused; resume first",
            })
            return
        entry = self._requests.setdefault(request_id, {})
        entry.update({"op": request.get("op", "run"), "steps": resolved, "cursor": 0})
        self._queue.extend(resolved)
        self.ledger.append(
            "sequence", request_id=request_id, event="accepted",
            steps=[{"skill": s["skill"], "sim_duration_s": s.get("sim_duration_s")}
                   for s in resolved],
        )
        self._write_response(request_id, {
            "status": "accepted",
            "steps": len(resolved),
            "session_id": self.session_id,
        })

    def _send_next_step(self) -> None:
        if self._active is not None or not self._queue:
            return
        step = self._queue.pop(0)
        if "closed_loop" in step:
            # anchor on the first snapshot; send the cruise/turn setpoint now
            if step["closed_loop"] == "distance":
                step["velocity"] = {"vx": closed_loop.CRUISE_VX, "vy": 0.0, "yaw": 0.0}
            else:
                import math
                rate = math.copysign(
                    closed_loop.TURN_RATE_MAX, step["controller"].target_rad)
                step["velocity"] = {"vx": 0.0, "vy": 0.0, "yaw": rate}
            step["sim_duration_s"] = None
        command = self._build_command(step)
        step["command_id"] = command["command_id"]
        step["sent_wall"] = time.monotonic()
        step["state"] = "sent"
        self._active = step
        self._write_command(command)

    def _drive_closed_loop(self) -> None:
        """§3.6 feedback: run the controller off fresh status snapshots."""
        active = self._active
        if (
            active is None
            or "closed_loop" not in active
            or active.get("state") != "applied"
        ):
            return
        status = pcf.read_json(self.paths.status)
        if status is None:
            return
        if time.time() - float(status.get("wall_updated_at", 0.0)) > STATUS_STALE_S:
            return  # 快照不新鲜:不发新指令(故障监控会处置)
        sim_time = float(status.get("sim_time_s", 0.0))
        quat = status.get("root_quat_w")
        pos = status.get("root_position_w")
        if quat is None or pos is None:
            return
        yaw = closed_loop.yaw_from_quat_wxyz(quat)
        controller = active["controller"]
        if active["closed_loop"] == "distance":
            vx, yaw_rate = controller.update((pos[0], pos[1]), yaw, sim_time)
        else:
            vx, yaw_rate = 0.0, controller.update(yaw, sim_time)
        if controller.status != "running":
            final = controller.final
            # zero the setpoint before reporting; never leave motion running
            active["velocity"] = {"vx": 0.0, "vy": 0.0, "yaw": 0.0}
            active["sim_duration_s"] = None
            zero = self._build_command(active)
            self._write_command(zero)
            request_id = active["request_id"]
            request = self._requests.get(request_id, {})
            is_last = active["step_index"] == len(request.get("steps", [])) - 1
            self.ledger.append(
                "sequence", request_id=request_id, event="closed_loop_end",
                step_index=active["step_index"], result=final,
            )
            if final["status"] == closed_loop.DONE:
                self._active = None
                if is_last:
                    self._write_response(request_id, {
                        "status": "completed", "result": final,
                    })
                else:
                    self._write_response(request_id, {
                        "status": "applied", "step_index": active["step_index"],
                        "note": "closed-loop step done; next step follows",
                    })
            else:
                # keep _active set so the failure response reaches the waiter
                self._fail_active_and_queue(
                    "failed",
                    f"closed-loop {final['status']}: {final}",
                )
            return
        current = active["velocity"]
        if abs(vx - current["vx"]) > 0.01 or abs(yaw_rate - current["yaw"]) > 0.02:
            active["velocity"] = {"vx": vx, "vy": 0.0, "yaw": yaw_rate}
            active["sim_duration_s"] = None
            command = self._build_command(active)  # new command_id supersedes
            active["command_id"] = command["command_id"]
            self._write_command(command)
        # progress observability (~1 Hz)
        now_m = time.monotonic()
        if now_m - active.get("_logged_progress_wall", 0.0) > 1.0:
            active["_logged_progress_wall"] = now_m
            ctrl = active["controller"]
            self.ledger.append(
                "sequence", request_id=active["request_id"], event="closed_loop_progress",
                step_index=active["step_index"],
                progress={
                    "yaw": round(yaw, 4),
                    "sim_time_s": sim_time,
                    "progress_m": getattr(ctrl, "last_progress", None),
                    "position": getattr(ctrl, "last_position", None),
                    "target_m": getattr(ctrl, "target_m", None),
                },
            )

    def _fail_active_and_queue(self, status: str, reason: str) -> None:
        """Abort the active step and drop the rest of its sequence."""
        affected = []
        if self._active is not None:
            affected.append(self._active)
            self._active = None
        affected.extend(self._queue)
        self._queue = []
        by_request: dict[str, list[dict]] = {}
        for step in affected:
            by_request.setdefault(step["request_id"], []).append(step)
        for request_id, steps in by_request.items():
            indexes = [s["step_index"] for s in steps]
            self.ledger.append(
                "sequence", request_id=request_id, event="aborted",
                reason=reason, remaining_steps=indexes,
            )
            self._write_response(request_id, {
                "status": status, "reason": reason,
                "failed_step": min(indexes),
            })

    def _handle_stop(self, request: dict) -> None:
        mode = request.get("mode", "stop_skill")
        request_id = request["request_id"]
        self._last_stop_wall = time.time()
        self._fail_active_and_queue("stopped", f"stop ({mode}): queued work invalidated")
        command = self._build_command({
            "control": mode if mode in ("stop_skill", "pause_sim", "cancel_session") else "stop_skill",
            "skill": "stop",
        })
        self._write_command(command)
        if mode == "pause_sim":
            self.state = "paused"
        if mode == "cancel_session":
            self.state = "cancelling"
        self._write_response(request_id, {"status": "stopped", "mode": mode})

    def _handle_resume(self, request: dict) -> None:
        if self.state != "paused":
            self._write_response(request["request_id"], {
                "status": "rejected", "classification": "policy_rejected",
                "reason": f"session is {self.state}, not paused",
            })
            return
        self.state = "running"
        command = self._build_command({"control": "resume_sim", "skill": "session"})
        self._write_command(command)
        self._write_response(request["request_id"], {"status": "applied", "mode": "resume_sim"})

    # -- ack/status processing -------------------------------------------------
    def _process_acks(self) -> None:
        payload = pcf.read_json(self.paths.ack)
        if payload is None or self._active is None:
            return
        active = self._active
        command_id = active.get("command_id")
        # rejected ACK for the active command -> step failed, abort sequence
        for ack in payload.get("acks", []):
            if ack.get("command_id") != command_id or ack.get("renewal"):
                continue
            key = f"ack:{command_id}:{ack['status']}"
            if active.get(key):
                continue
            active[key] = True
            self.ledger.append(
                "ack", session_id=self.session_id, command_id=command_id,
                status=ack["status"], applied_step=ack.get("applied_step"),
                sim_time_s=ack.get("sim_time_s"), reason=ack.get("reason", ""),
            )
            if ack["status"] == "rejected":
                self._fail_active_and_queue(
                    "failed", f"step rejected by runtime: {ack.get('reason', '')}"
                )
                return
            if ack["status"] == "applied" and active["state"] == "sent":
                active["state"] = "applied"
                active["applied_wall"] = time.monotonic()
                request = self._requests.get(active["request_id"], {})
                total = len(request.get("steps", []))
                is_last = active["step_index"] == total - 1
                self._write_response(active["request_id"], {
                    "status": "applied",
                    "step_index": active["step_index"],
                    "steps": total,
                    "command_id": command_id,
                    "applied_step": ack.get("applied_step"),
                    "sim_time_s": ack.get("sim_time_s"),
                })
                if "control" in active:
                    # stop/pause/resume steps complete at the applied ACK
                    request_id_a = active["request_id"]
                    self.ledger.append(
                        "sequence", request_id=request_id_a, event="step_completed",
                        step_index=active["step_index"], command_id=command_id,
                        note="control step completes at applied ACK",
                    )
                    self._active = None
                    if is_last:
                        result = {"status": "applied", "command_id": command_id}
                        motion = request.get("last_step_result")
                        if motion is not None:
                            result["motion_step"] = motion
                        self._write_response(request_id_a, {
                            "status": "completed", "steps": total, "result": result,
                        })
                    return
                if (
                    active.get("sim_duration_s") is None
                    and not is_last
                    and "closed_loop" not in active  # 闭环步由 _drive_closed_loop 收尾
                ):
                    # streaming step inside a sequence: supersede with the next
                    self._active = None
                    return
        result = payload.get("results", {}).get(command_id)
        if result is None or self._active is None:
            return
        status = result["status"]
        request_id = active["request_id"]
        request = self._requests.get(request_id, {})
        steps = request.get("steps", [])
        is_last = active["step_index"] == len(steps) - 1
        if status == "completed":
            self.ledger.append(
                "sequence", request_id=request_id, event="step_completed",
                step_index=active["step_index"], command_id=command_id,
                sim_steps_executed=result.get("sim_steps_executed"),
            )
            if request_id in self._requests:
                self._requests[request_id]["last_step_result"] = result
            self._active = None
            if is_last:
                self._write_response(request_id, {
                    "status": "completed", "steps": len(steps), "result": result,
                })
            # next step is sent by the main loop only now (ACK+result seen)
        else:
            # interrupted_by_reset / stopped / any non-completed result aborts
            self.ledger.append(
                "sequence", request_id=request_id, event="step_interrupted",
                step_index=active["step_index"], result_status=status,
                reason=result.get("reason", ""),
            )
            mapped = "interrupted" if status == "interrupted_by_reset" else "failed"
            self._fail_active_and_queue(mapped, result.get("reason", status))

    def _check_wall_timeouts(self) -> None:
        """Wall-clock budget per step: independent of sim-time completion."""
        active = self._active
        if active is None or active.get("state") != "applied":
            return
        duration = active.get("sim_duration_s")
        if duration is None:
            return
        budget = duration * self.step_wall_timeout_factor + 5.0
        if time.monotonic() - active["applied_wall"] > budget:
            self._fail_active_and_queue(
                "failed", f"wall timeout: {duration}s sim step exceeded {budget:.0f}s wall"
            )

    def _renew_lease_and_heartbeat(self) -> None:
        active = self._active
        now = time.monotonic()
        if os.environ.get("MICRODUCK_NO_RENEWAL"):
            pass  # debug kill-switch
        elif (
            active is not None
            and active.get("state") == "applied"
            and active.get("sim_duration_s") is None
        ):
            # streaming command: renew before the sim-time lease runs out
            status = pcf.read_json(self.paths.status) or {}
            sim_time = status.get("sim_time_s")
            if sim_time is not None:
                deadline = active.get("lease_deadline_sim")
                if deadline is None:
                    deadline = sim_time + self.streaming_lease_s
                    active["lease_deadline_sim"] = deadline
                if sim_time > deadline - self.streaming_lease_s / 2:
                    renew = self._build_command({
                        "velocity": active["velocity"],
                        "sim_duration_s": None,
                        "skill": active["skill"],
                    })
                    renew["command_id"] = active["command_id"]  # same id: renewal
                    self._write_command(renew)
                    active["lease_deadline_sim"] = sim_time + self.streaming_lease_s
                    return
        if (
            now - self._last_write_wall > HEARTBEAT_PERIOD_S
            and self.paths.command.exists()
            and not os.environ.get("MICRODUCK_NO_HEARTBEAT")  # debug kill-switch
        ):
            # heartbeat: rewrite unchanged content so the play-side watchdog
            # sees a live writer even while idle
            payload = pcf.read_json(self.paths.command)
            if payload is not None:
                pcf.atomic_write_json(self.paths.command, payload)
                self._last_write_wall = now

    def _watch_play(self) -> None:
        status = pcf.read_json(self.paths.status)
        if status is not None:
            self._status_seen = True
            if self.state == "starting":
                self.state = "running"
                self.ledger.append(
                    "session", event="running", session_id=self.session_id,
                    backend=self.backend,
                )
                self._write_meta()
            fresh = time.time() - float(status.get("wall_updated_at", 0.0)) < STATUS_STALE_S
            if fresh and status.get("run_state") == "fault_stopped" and self.state != "fault":
                self.state = "fault"
                self.ledger.append(
                    "fault", session_id=self.session_id,
                    reason=status.get("fault_reason"), source="play_status",
                )
                self._fail_active_and_queue(
                    "failed", f"play fault-stop: {status.get('fault_reason')}"
                )
                self._write_meta()
            elif self.state == "fault" and status.get("run_state") != "fault_stopped":
                self.state = "running"
                self._write_meta()
            if self._status_seen and not fresh and self.state == "running":
                # stale snapshot is never stop evidence; it is a freshness fault
                self.state = "fault"
                self.ledger.append(
                    "fault", session_id=self.session_id,
                    reason="status_snapshot_stale", source="watch",
                )
                self._fail_active_and_queue("failed", "status snapshot stale")
                self._write_meta()
        if self._process is not None and self._process.poll() is not None:
            exit_code = self._process.returncode
            if self.state == "cancelling":
                self.state = "cancelled"
            elif self.state != "cancelled":
                self.state = "exited"
            self.ledger.append(
                "session", event=self.state, session_id=self.session_id,
                exit_code=exit_code, backend=self.backend,
            )
            self._write_meta()
            self._fail_active_and_queue("failed", f"play process exited ({exit_code})")
            self._shutdown = True

    # -- cancel ----------------------------------------------------------------
    def _kill_play(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        for sig, grace in ((signal.SIGINT, 3.0), (signal.SIGTERM, 3.0), (signal.SIGKILL, 5.0)):
            if process.poll() is not None:
                return
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)

    # -- main loop ---------------------------------------------------------------
    def run(self) -> int:
        self.start_play()
        try:
            while not self._shutdown:
                # stop is processed before anything queued; never blocked
                for request in self._scan_requests():
                    if not request.get("request_id"):
                        continue
                    created = float(request.get("created_at", time.time()))
                    op = request.get("op")
                    if op in ("run", "sequence") and created < self._last_stop_wall:
                        # late arrival constructed before the stop: invalid
                        self.ledger.append(
                            "sequence", request_id=request.get("request_id"),
                            event="late_request_dropped", created_at=created,
                        )
                        self._write_response(request["request_id"], {
                            "status": "stopped",
                            "reason": "request predates the last stop; invalidated",
                        })
                        continue
                    if created < self._started_wall:
                        # stale request file from a previous session on this dir
                        self._write_response(request["request_id"], {
                            "status": "rejected", "classification": "policy_rejected",
                            "reason": "stale request predates this session process",
                        })
                        continue
                    if op == "stop":
                        self._handle_stop(request)
                    elif op == "resume":
                        self._handle_resume(request)
                    elif op in ("run", "sequence"):
                        self._submit_steps(request)
                self._process_acks()
                self._drive_closed_loop()
                self._check_wall_timeouts()
                self._send_next_step()
                self._renew_lease_and_heartbeat()
                self._watch_play()
                if self.state == "cancelling":
                    # wait briefly for the play process to honour cancel_session
                    if self._process is not None and self._process.poll() is None:
                        waited = getattr(self, "_cancel_waited", 0.0) + LOOP_PERIOD_S
                        self._cancel_waited = waited
                        if waited > CANCEL_GRACE_S:
                            self._kill_play()
                time.sleep(LOOP_PERIOD_S)
        finally:
            if self._process is not None and self._process.poll() is None:
                self._kill_play()
            if self.state not in ("cancelled", "exited"):
                self.state = "cancelled"
                self._write_meta()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--backend", choices=("isaac", "stub"), default="stub")
    parser.add_argument("--name", default="sim01")
    parser.add_argument("--scene", default="flat")
    parser.add_argument("--steps", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--stub-option", action="append", default=[],
                        help="key=value test-double options (control_dt, tick_sleep, ...)")
    parser.add_argument("--streaming-lease-s", type=float, default=STREAMING_LEASE_S)
    args = parser.parse_args(argv)
    stub_options = {}
    for item in args.stub_option:
        key, _, value = item.partition("=")
        stub_options[key] = float(value)
    service = SessionService(
        session_dir=args.session_dir,
        registry_path=args.registry,
        ledger_path=args.ledger,
        backend=args.backend,
        name=args.name,
        scene=args.scene,
        steps=args.steps,
        seed=args.seed,
        stub_options=stub_options,
        streaming_lease_s=args.streaming_lease_s,
    )
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
