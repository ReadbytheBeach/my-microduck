#!/usr/bin/env python3
"""Structured CLI (mdctl style) for MicroDuck language/CLI sim control.

    mdctl session start --backend isaac --scene flat --name sim01
    mdctl skills list --session sim01
    mdctl skill run walk --session sim01 --vx 0.10 --duration-sim 2 --execute
    mdctl say --session sim01 "向前走两秒，然后停下" --execute
    mdctl skill stop --session sim01
    mdctl status --session sim01 --json

The CLI is a *client* of the single session control service: it writes
request files and reads responses; the service (language_control/
session_service.py) owns the command file. The structured CLI needs no API
key and no LLM; `say` uses the local rule grammar by default (M1-B) and
only calls Kimi with an explicit --llm.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playback_command_feed as pcf  # noqa: E402

from .intent import SequenceParser, KimiClient, STOP_PHRASES  # noqa: E402
from .proposal import ProposalError  # noqa: E402
from .session_service import (  # noqa: E402
    DEFAULT_LEDGER,
    DEFAULT_REGISTRY,
    DEFAULT_SESSIONS_ROOT,
    TERMINAL_STATES,
)


def session_dir(name: str, sessions_root: Path = DEFAULT_SESSIONS_ROOT) -> Path:
    return Path(sessions_root) / name


def _write_request(sdir: Path, request: dict) -> str:
    request_id = uuid.uuid4().hex
    request = {"request_id": request_id, "created_at": time.time(), **request}
    pcf.atomic_write_json(sdir / "requests" / f"{request_id}.json", request)
    return request_id


def _wait_response(
    sdir: Path,
    request_id: str,
    timeout_s: float,
    wait_result: bool = False,
) -> dict:
    """Poll the response file; return the first applied/terminal state."""
    path = sdir / "responses" / f"{request_id}.json"
    deadline = time.monotonic() + timeout_s
    seen = None
    while time.monotonic() < deadline:
        payload = pcf.read_json(path)
        if payload is not None:
            seen = payload
            status = payload.get("status")
            if status in TERMINAL_STATES:
                return payload
            if not wait_result and status == "applied":
                return payload
        meta = _load_meta(sdir)
        if meta is not None and meta.get("state") in ("exited", "cancelled"):
            return {
                "request_id": request_id,
                "status": "failed",
                "reason": (
                    f"session is {meta['state']}; the playback process has ended. "
                    "Start a new session with: mdctl session start"
                ),
            }
        time.sleep(0.05)
    return seen or {
        "request_id": request_id,
        "status": "timeout",
        "reason": f"no response within {timeout_s:.0f}s",
    }


def _load_meta(sdir: Path) -> dict | None:
    return pcf.read_json(sdir / "session.json")


def _require_session(name: str, sessions_root: Path) -> tuple[Path, dict]:
    sdir = session_dir(name, sessions_root)
    meta = _load_meta(sdir)
    if meta is None:
        raise SystemExit(f"session {name!r} not found under {sessions_root}; start it first")
    return sdir, meta


def _require_live_session(name: str, sessions_root: Path) -> tuple[Path, dict]:
    sdir, meta = _require_session(name, sessions_root)
    state = meta.get("state")
    if state in ("exited", "cancelled"):
        raise SystemExit(
            f"session {name!r} is {state} (playback ended, e.g. step budget spent); "
            f"start a new one: mdctl session start --backend {meta.get('backend', 'stub')} --name {name}"
        )
    return sdir, meta


# -- subcommands -----------------------------------------------------------


def cmd_session_start(args) -> dict:
    sdir = session_dir(args.name, args.sessions_root)
    meta = _load_meta(sdir)
    if meta is not None and meta.get("state") in ("starting", "running", "paused", "fault"):
        pid = meta.get("pid_service")
        if pid and _pid_alive(pid):
            raise SystemExit(f"session {args.name!r} already running (pid {pid})")
    # clear stale protocol files so the startup wait never reads last
    # session's state
    for stale in ("session.json", "status.json", "ack.json", "command.json"):
        try:
            (sdir / stale).unlink()
        except FileNotFoundError:
            pass
    for stale_dir in ("requests", "responses"):
        for stale_file in (sdir / stale_dir).glob("*.json"):
            stale_file.unlink()
    # stale video artifacts from a previous run on this session name:
    # the web panel must only show this session's clips
    for stale in ("session.mp4", "latest.jpg", "latest.jpg.tmp"):
        try:
            (sdir / stale).unlink()
        except FileNotFoundError:
            pass
    for stale_clip in (sdir / "clips").glob("*.mp4"):
        stale_clip.unlink()
    sdir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, "-m", "language_control.session_service",
        "--session-dir", str(sdir),
        "--registry", str(args.registry),
        "--ledger", str(args.ledger),
        "--backend", args.backend,
        "--name", args.name,
        "--scene", args.scene,
        "--steps", str(args.steps),
        "--seed", str(args.seed),
    ]
    lease = args.streaming_lease_s
    if lease is None:
        # a full-speed stub outruns the 50 ms renewal loop in sim time;
        # wall-clock writer liveness is still guarded by the file watchdog
        lease = 3600.0 if args.backend == "stub" else 3.0
    argv += ["--streaming-lease-s", str(lease)]
    if args.video:
        argv += ["--video"]
    for key, value in (args.stub_option or []):
        argv += ["--stub-option", f"{key}={value}"]
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    if os.environ.get("MICRODUCK_DEBUG_OBS"):  # debug passthrough
        env["MICRODUCK_DEBUG_OBS"] = "1"
    for _debug_name in ("MICRODUCK_NO_HEARTBEAT", "MICRODUCK_NO_RENEWAL"):
        if os.environ.get(_debug_name):
            env[_debug_name] = "1"
    log = (sdir / "daemon.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        argv,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    timeout = args.startup_timeout if args.startup_timeout else (
        300.0 if args.backend == "isaac" else 30.0
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = _load_meta(sdir)
        if meta is not None and meta.get("state") in ("running", "paused", "fault"):
            return {
                "started": True,
                "name": args.name,
                "backend": args.backend,
                "session_id": meta["session_id"],
                "session_epoch": meta["session_epoch"],
                "pid_service": process.pid,
                "policy_sha256": meta.get("policy_sha256"),
                "state": meta["state"],
            }
        if meta is not None and meta.get("state") in ("exited", "cancelled"):
            raise SystemExit(
                f"session {args.name!r} play process died at startup; see {sdir/'play.log'}"
            )
        time.sleep(0.2)
    raise SystemExit(f"session {args.name!r} did not become running within {timeout:.0f}s")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmd_session_stop(args) -> dict:
    sdir, meta = _require_session(args.name, args.sessions_root)
    request_id = _write_request(sdir, {"op": "stop", "mode": "cancel_session"})
    response = _wait_response(sdir, request_id, timeout_s=30.0)
    return {"session": args.name, "cancel_session": response}


def cmd_skills_list(args) -> dict:
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    session_model = None
    if args.session:
        _sdir, meta = _require_session(args.session, args.sessions_root)
        session_model = meta.get("policy_model")
    skills = {}
    walk_available = (
        registry["skills"].get("walk", {}).get("usage_state") == "available"
    )
    for name, record in sorted(registry["skills"].items()):
        encoding = record.get("command", {}).get("encoding")
        model = record.get("model", {}).get("path")
        if record.get("kind") == "builtin":
            executable = True
        elif encoding != "velocity":
            executable = False
        elif name == "stand" and session_model is not None and model != session_model:
            # stand runs as the verified same-policy zero-velocity switch
            executable = walk_available
        else:
            executable = (
                record.get("usage_state") == "available"
                and (session_model is None or model == session_model)
            )
        skills[name] = {
            "usage_state": record.get("usage_state"),
            "evidence_state": record.get("evidence_state"),
            "encoding": encoding,
            "session_executable": executable,
            "note": (
                "same-policy zero-velocity switch on the session policy"
                if name == "stand" and model != session_model else ""
            ),
        }
    return {"session": args.session, "skills": skills}


def cmd_skill_run(args) -> dict:
    sdir, meta = _require_live_session(args.session, args.sessions_root)
    params: dict = {}
    for name in ("vx", "vy", "yaw"):
        value = getattr(args, name)
        if value is not None:
            params[name] = value
    if args.distance_m is not None:
        params["distance_m"] = args.distance_m
    if args.turn_deg is not None:
        params["turn_deg"] = args.turn_deg
    if args.duration_sim is not None:
        params["sim_duration_s"] = args.duration_sim
    request_id = _write_request(sdir, {
        "op": "run",
        "source": "cli",
        "steps": [{"skill": args.skill, "params": params}],
    })
    response = _wait_response(
        sdir, request_id, timeout_s=args.timeout, wait_result=args.wait_result
    )
    return {"session": args.session, "response": response}


def cmd_say(args) -> dict:
    sdir, meta = _require_live_session(args.session, args.sessions_root)
    text = args.text
    normalized = "".join(text.split())
    # stop is local and immediate — parsed without any LLM, sent as a stop
    # request, never queued behind anything
    if normalized in STOP_PHRASES:
        request_id = _write_request(sdir, {"op": "stop", "mode": "stop_skill"})
        response = _wait_response(sdir, request_id, timeout_s=args.timeout)
        return {"session": args.session, "handled": "stop", "response": response}
    try:
        if args.llm:
            proposal = KimiClient().parse(text)  # single-step LLM path
        else:
            proposal = SequenceParser().parse(text)
    except ProposalError as exc:
        return {
            "session": args.session,
            "handled": "rejected",
            "classification": exc.classification,
            "reason": exc.reason,
        }
    request_id = _write_request(sdir, {
        "op": "sequence",
        "source": "llm" if args.llm else "rule",
        "raw_text": text,
        "proposal_id": proposal.proposal_id,
        "steps": [step.to_dict() for step in proposal.steps],
    })
    response = _wait_response(
        sdir, request_id, timeout_s=args.timeout, wait_result=args.wait_result
    )
    return {"session": args.session, "handled": "submitted", "response": response}


def cmd_skill_stop(args) -> dict:
    sdir, meta = _require_live_session(args.session, args.sessions_root)
    mode = "cancel_session" if args.cancel else ("pause_sim" if args.pause else "stop_skill")
    request_id = _write_request(sdir, {"op": "stop", "mode": mode})
    response = _wait_response(sdir, request_id, timeout_s=args.timeout)
    return {"session": args.session, "mode": mode, "response": response}


def cmd_status(args) -> dict:
    sdir, meta = _require_session(args.session, args.sessions_root)
    status = pcf.read_json(sdir / "status.json")
    ack = pcf.read_json(sdir / "ack.json")
    result = {"session": args.session, "meta": meta}
    if status is None:
        result["status"] = None
        result["fresh"] = False
    else:
        age = time.time() - float(status.get("wall_updated_at", 0.0))
        result["status"] = status
        result["snapshot_age_s"] = age
        # a stale snapshot is never evidence of a fresh stop
        result["fresh"] = age < 3.0
    if ack is not None:
        result["recent_acks"] = ack.get("acks", [])[-5:]
        result["results"] = ack.get("results", {})
    return result


# -- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mdctl", description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    start = session_sub.add_parser("start")
    start.add_argument("--backend", choices=("isaac", "stub"), default="isaac")
    start.add_argument("--scene", default="flat")
    start.add_argument("--name", default="sim01")
    start.add_argument("--steps", type=int, default=2_000_000)
    start.add_argument("--seed", type=int, default=42)
    start.add_argument("--startup-timeout", type=float, default=None)
    start.add_argument("--stub-option", action="append", nargs=2, metavar=("KEY", "VALUE"))
    start.add_argument("--streaming-lease-s", type=float, default=None,
                       help="sim-time lease for streaming commands "
                            "(default: 3.0 isaac / 3600.0 stub)")
    start.add_argument("--video", action="store_true",
                       help="isaac backend: record session.mp4 + per-command clips "
                            "and a ~1 Hz preview frame (GPU cost)")
    start.set_defaults(func=cmd_session_start)
    stop = session_sub.add_parser("stop")
    stop.add_argument("--name", default="sim01")
    stop.set_defaults(func=cmd_session_stop)

    skills = sub.add_parser("skills")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    list_parser = skills_sub.add_parser("list")
    list_parser.add_argument("--session", default=None)
    list_parser.set_defaults(func=cmd_skills_list)

    skill = sub.add_parser("skill")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    run = skill_sub.add_parser("run")
    run.add_argument("skill")
    run.add_argument("--session", required=True)
    run.add_argument("--vx", type=float, default=None)
    run.add_argument("--vy", type=float, default=None)
    run.add_argument("--yaw", type=float, default=None)
    run.add_argument("--duration-sim", type=float, default=None)
    run.add_argument("--distance-m", type=float, default=None,
                     help="closed-loop move_distance (sim_ground_truth feedback)")
    run.add_argument("--turn-deg", type=float, default=None,
                     help="closed-loop relative heading turn (angle-wrap safe)")
    run.add_argument("--execute", action="store_true",
                     help="kept for mdctl-example parity; execution is the default here")
    run.add_argument("--wait-result", action="store_true",
                     help="wait for the terminal result, not just the applied ACK")
    run.add_argument("--timeout", type=float, default=120.0)
    run.set_defaults(func=cmd_skill_run)
    stop_skill = skill_sub.add_parser("stop")
    stop_skill.add_argument("--session", required=True)
    stop_skill.add_argument("--pause", action="store_true", help="pause_sim instead of stop_skill")
    stop_skill.add_argument("--cancel", action="store_true", help="cancel_session (ends the session)")
    stop_skill.add_argument("--timeout", type=float, default=30.0)
    stop_skill.set_defaults(func=cmd_skill_stop)

    say = sub.add_parser("say")
    say.add_argument("--session", required=True)
    say.add_argument("text")
    say.add_argument("--execute", action="store_true", help="parity with the task-book example")
    say.add_argument("--llm", action="store_true", help="parse via Kimi (needs MOONSHOT_API_KEY)")
    say.add_argument("--wait-result", action="store_true")
    say.add_argument("--timeout", type=float, default=120.0)
    say.set_defaults(func=cmd_say)

    status = sub.add_parser("status")
    status.add_argument("--session", required=True)
    status.add_argument("--json", action="store_true", help="parity flag; output is always JSON")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = args.func(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
