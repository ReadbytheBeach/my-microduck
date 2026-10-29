#!/usr/bin/env python3
"""Isaac-free test double for the M1 session playback loop.

Implements the play side of the command/ACK protocol using the real
scripts/playback_command_feed.py state machine, with a kinematic fake robot
(position integrates the applied velocity). Used by the session-service
tests so every protocol behaviour is exercised without Isaac. NOT a
production entry point: physics, policy and contacts are faked.

Extra test hooks: --episode-steps simulates the 20 s time_out boundary with
a short episode; --tick-sleep forces a slower-than-realtime "slow sim";
--reject-vx makes the runtime reject a matching vx (dynamic failure path).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playback_command_feed import CommandFeed, write_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-file", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--ack-file", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-epoch", type=int, default=1)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--control-dt", type=float, default=0.02)
    parser.add_argument("--tick-sleep", type=float, default=0.0,
                        help="wall seconds to sleep per control step (slow sim)")
    parser.add_argument("--episode-steps", type=int, default=0,
                        help="if >0, force a time_out reset every N steps")
    parser.add_argument("--reject-vx", type=float, default=None,
                        help="test hook: reject velocity_setpoint whose vx matches")
    parser.add_argument("--freeze", type=float, default=0.0,
                        help="test hook: if nonzero, command applies but the body "
                             "never moves (standing-still attractor)")
    parser.add_argument("--policy-sha256", default="0" * 64)
    parser.add_argument("--watchdog-s", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    feed = CommandFeed(
        args.command_file,
        session_id=args.session_id,
        session_epoch=args.session_epoch,
        control_dt_s=args.control_dt,
        ack_path=args.ack_file,
        watchdog_wall_s=args.watchdog_s,
    )

    episode_id = 0
    done_count = 0
    position = [0.0, 0.0, 0.12]
    yaw = 0.0
    velocity = (0.0, 0.0, 0.0)
    step = 0
    cancelled = False

    def status_payload(run_state):
        import math as _math
        return {
            "protocol_version": 1,
            "session_id": args.session_id,
            "session_epoch": args.session_epoch,
            "sim_step": step,
            "sim_time_s": step * args.control_dt,
            "episode_id": episode_id,
            "run_state": run_state,
            "fault_reason": feed.fault,
            "applied_command_id": feed.applied_command_id,
            "policy_sha256": args.policy_sha256,
            "current_velocity": list(velocity),
            "root_position_w": list(position),
            "root_quat_w": [_math.cos((yaw + _math.pi) / 2), 0.0, 0.0, _math.sin((yaw + _math.pi) / 2)],
            "root_lin_vel_b": [velocity[0], velocity[1], 0.0],
            "done_count": done_count,
            "reset_reason": feed.reset_reason,
            "wall_updated_at": time.time(),
        }

    print("MICRODUCK_PLAYBACK_CHECKPOINT=stub", flush=True)
    # hold at step 0 until the first command (mirrors the real play script)
    hold_deadline = time.monotonic() + 30.0
    while feed.applied_command_id is None and not feed.cancel_requested:
        feed.poll(sim_step=0, sim_time_s=0.0)
        write_status(args.status_file, status_payload("waiting_first_command"))
        if time.monotonic() > hold_deadline:
            break
        time.sleep(0.005)
    while step < args.steps:
        sim_time = step * args.control_dt
        feed.poll(sim_step=step, sim_time_s=sim_time)
        # dynamic-failure test hook: the runtime rejects a matching command
        if (
            args.reject_vx is not None
            and feed._active is not None
            and abs(feed._active.command.velocity()[0] - args.reject_vx) < 1e-9
        ):
            rejected = feed._active.command
            feed._active = None
            # drop the transient applied ACK so the final file carries only
            # the rejection for this command_id
            feed.acks = [a for a in feed.acks if a["command_id"] != rejected.command_id]
            feed.acks.append({
                "command_id": rejected.command_id,
                "sequence": rejected.sequence,
                "skill_id": rejected.skill_id,
                "kind": rejected.kind,
                "status": "rejected",
                "reason": "stub --reject-vx test hook",
                "applied_step": step,
                "sim_time_s": sim_time,
                "renewal": False,
            })
            feed.results[rejected.command_id] = {
                "status": "rejected",
                "reason": "stub --reject-vx test hook",
                "command_id": rejected.command_id,
            }
            feed._acks_dirty = True
            feed._flush_acks(step, sim_time)
        if feed.cancel_requested:
            cancelled = True
            break
        if feed.paused:
            import math as _math
            quat = [_math.cos((yaw + _math.pi) / 2), 0.0, 0.0, _math.sin((yaw + _math.pi) / 2)]  # 镜像真实资产的根坐标系 π 偏移
            write_status(args.status_file, {
                "protocol_version": 1,
                "session_id": args.session_id,
                "session_epoch": args.session_epoch,
                "sim_step": step,
                "sim_time_s": sim_time,
                "episode_id": episode_id,
                "run_state": feed.run_state,
                "fault_reason": feed.fault,
                "applied_command_id": feed.applied_command_id,
                "policy_sha256": args.policy_sha256,
                "current_velocity": [0.0, 0.0, 0.0],
                "root_position_w": list(position),
                "root_quat_w": quat,
                "root_lin_vel_b": [0.0, 0.0, 0.0],
                "done_count": done_count,
                "reset_reason": feed.reset_reason,
                "wall_updated_at": time.time(),
            })
            time.sleep(0.005)
            continue
        velocity = feed.current_velocity()
        # planar motion in the current heading; yaw integrates yaw_rate
        import math as _math
        if args.freeze:
            effective = (0.0, 0.0, 0.0)  # attractor: command applied, no motion
        else:
            effective = velocity
        position[0] += effective[0] * _math.cos(yaw) * args.control_dt
        position[1] += effective[0] * _math.sin(yaw) * args.control_dt
        position[1] += effective[1] * args.control_dt
        yaw += effective[2] * args.control_dt
        step += 1
        if args.episode_steps > 0 and step % args.episode_steps == 0 and step < args.steps:
            episode_id += 1
            done_count += 1
            position = [0.0, 0.0, 0.12]  # reset pose, like env auto-reset
            yaw = 0.0
            feed.on_episode_reset(episode_id, "time_out", step, step * args.control_dt)
        quat = [_math.cos((yaw + _math.pi) / 2), 0.0, 0.0, _math.sin((yaw + _math.pi) / 2)]  # 镜像真实资产的根坐标系 π 偏移
        write_status(args.status_file, {
            "protocol_version": 1,
            "session_id": args.session_id,
            "session_epoch": args.session_epoch,
            "sim_step": step,
            "sim_time_s": step * args.control_dt,
            "episode_id": episode_id,
            "run_state": feed.run_state,
            "fault_reason": feed.fault,
            "applied_command_id": feed.applied_command_id,
            "policy_sha256": args.policy_sha256,
            "current_velocity": list(velocity),
            "root_position_w": list(position),
            "root_quat_w": quat,
            "root_lin_vel_b": [effective[0], effective[1], 0.0],
            "done_count": done_count,
            "reset_reason": feed.reset_reason,
            "wall_updated_at": time.time(),
        })
        if args.tick_sleep > 0:
            time.sleep(args.tick_sleep)

    summary = {
        "task": "stub-session",
        "session_id": args.session_id,
        "session_epoch": args.session_epoch,
        "steps_executed": step,
        "cancelled": cancelled,
        "done_count": done_count,
        "episode_id": episode_id,
        "final_position": position,
        "results": feed.results,
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("MICRODUCK_PLAYBACK_STAGE=complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
