#!/usr/bin/env python3
"""Closed-loop distance/heading control for velocity sessions (task book §3.6).

Pure logic module (Isaac-free): the session service feeds it status
snapshots (root position/orientation, sim time) and it returns velocity
setpoints. Position source is labeled sim_ground_truth — the sim root pose,
NOT real-robot localization.

- move_distance: fix start p0 and initial planar heading u0; progress
  s(t) = u0·(p(t)-p0); lateral error |u0⟂·(p(t)-p0)|, heading drift,
  speed and sim-time timeout are all bounded; two-phase speed (cruise then
  creep near the target); terminal report carries the final endpoint error.
- turn_deg: relative-heading feedback with angle wrapping, not
  yaw_rate × time.

This is the closed loop. Where no feedback source exists the service must
refuse or explicitly use the labeled estimation mode — never silently
substitute open-loop estimates for a requested closed loop.
"""

from __future__ import annotations

import math


CRUISE_VX = 0.15          # m/s, within registry walk limits
PROGRESS_TOLERANCE_M = 0.01
LATERAL_ERROR_MAX_M = 0.10
HEADING_DRIFT_MAX_RAD = math.radians(30.0)
TURN_RATE_MAX = 0.8       # rad/s, within yaw limit ±1.0
TURN_TOLERANCE_RAD = math.radians(2.0)
TURN_TIMEOUT_SIM_S = 15.0
DISTANCE_TIMEOUT_FACTOR = 4.0   # budget = distance / cruise * factor (sim time)
MIN_DISTANCE_M = 0.05
MAX_DISTANCE_M = 1.0
MIN_TURN_DEG = 5.0
MAX_TURN_DEG = 180.0

DONE = "done"
TIMEOUT = "timeout"
LATERAL_FAULT = "lateral_fault"
HEADING_FAULT = "heading_fault"


# 实测(2026-10-27, Isaac 会话): 机器人资产根坐标系相对机体前向偏转 π,
# root_quat_w(wxyz) 在 spawn 时读数 yaw=π 而机体朝 +x 行走。航向读数统一加
# 此偏移;相对量(漂移/转角)不受影响。
YAW_OFFSET_RAD = math.pi


def wrap_angle(angle: float) -> float:
    """Wrap to (-pi, pi] — 角度环绕处理."""
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def yaw_from_quat_wxyz(quat: list[float]) -> float:
    w, x, y, z = (float(v) for v in quat)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class DistanceController:
    """move_distance closed loop over sim_ground_truth root snapshots."""

    def __init__(self, distance_m: float):
        if not MIN_DISTANCE_M <= distance_m <= MAX_DISTANCE_M:
            raise ValueError(
                f"distance {distance_m} outside closed-loop range "
                f"[{MIN_DISTANCE_M}, {MAX_DISTANCE_M}] m"
            )
        self.target_m = distance_m
        self.p0: tuple[float, float] | None = None
        self.u0: tuple[float, float] | None = None
        self.yaw0: float | None = None
        self.started_sim_time_s: float | None = None
        self.status = "running"
        self.final: dict | None = None
        self.last_progress: float | None = None
        self.last_position: tuple[float, float] | None = None

    def update(
        self,
        position_xy: tuple[float, float],
        yaw: float,
        sim_time_s: float,
    ) -> tuple[float, float]:
        """Return (vx, yaw_rate). First call anchors p0/u0/yaw0."""
        if self.status != "running":
            return (0.0, 0.0)
        if self.p0 is None:
            self.p0 = (float(position_xy[0]), float(position_xy[1]))
            heading = wrap_angle(yaw + YAW_OFFSET_RAD)
            self.u0 = (math.cos(heading), math.sin(heading))
            self.yaw0 = yaw
            self.started_sim_time_s = sim_time_s
            return (CRUISE_VX, 0.0)
        dx = position_xy[0] - self.p0[0]
        dy = position_xy[1] - self.p0[1]
        progress = self.u0[0] * dx + self.u0[1] * dy
        lateral = -self.u0[1] * dx + self.u0[0] * dy
        heading_drift = abs(wrap_angle(yaw - self.yaw0))
        remaining = self.target_m - progress
        self.last_progress = progress
        self.last_position = (float(position_xy[0]), float(position_xy[1]))

        if progress >= self.target_m - PROGRESS_TOLERANCE_M:
            self.status = DONE
        elif abs(lateral) > LATERAL_ERROR_MAX_M:
            self.status = LATERAL_FAULT
        elif heading_drift > HEADING_DRIFT_MAX_RAD:
            self.status = HEADING_FAULT
        elif sim_time_s - self.started_sim_time_s > (
            self.target_m / CRUISE_VX * DISTANCE_TIMEOUT_FACTOR
        ):
            self.status = TIMEOUT

        if self.status != "running":
            self.final = {
                "status": self.status,
                "target_m": self.target_m,
                "progress_m": progress,
                "endpoint_error_m": remaining,
                "lateral_error_m": lateral,
                "heading_drift_rad": wrap_angle(yaw - self.yaw0),
                "sim_duration_s": sim_time_s - self.started_sim_time_s,
                "position_source": "sim_ground_truth",
            }
            return (0.0, 0.0)
        # 全程巡航速度: 实测发现中途降到爬行速度(0.05)会让 velstand 失速进入
        # 站立不动吸引子; 终点误差 = 制动过冲, 如实报告
        yaw_cmd = max(-0.3, min(0.3, -1.0 * wrap_angle(yaw - self.yaw0)))
        return (CRUISE_VX, yaw_cmd)


class TurnController:
    """左转/右转 N 度: 相对航向反馈,处理角度环绕."""

    def __init__(self, turn_deg: float):
        if not MIN_TURN_DEG <= abs(turn_deg) <= MAX_TURN_DEG:
            raise ValueError(
                f"turn {turn_deg} outside closed-loop range "
                f"±[{MIN_TURN_DEG}, {MAX_TURN_DEG}] deg"
            )
        self.target_rad = math.radians(turn_deg)
        self.yaw0: float | None = None
        self.started_sim_time_s: float | None = None
        self.status = "running"
        self.final: dict | None = None

    def update(self, yaw: float, sim_time_s: float) -> float:
        """Return yaw_rate setpoint."""
        if self.status != "running":
            return 0.0
        if self.yaw0 is None:
            self.yaw0 = yaw
            self.started_sim_time_s = sim_time_s
            return math.copysign(TURN_RATE_MAX, self.target_rad)
        turned = wrap_angle(yaw - self.yaw0)
        error = wrap_angle(self.target_rad - turned)
        if abs(error) <= TURN_TOLERANCE_RAD:
            self.status = DONE
        elif sim_time_s - self.started_sim_time_s > TURN_TIMEOUT_SIM_S:
            self.status = TIMEOUT
        if self.status != "running":
            self.final = {
                "status": self.status,
                "target_deg": math.degrees(self.target_rad),
                "turned_deg": math.degrees(turned),
                "heading_error_deg": math.degrees(error),
                "sim_duration_s": sim_time_s - self.started_sim_time_s,
                "position_source": "sim_ground_truth",
            }
            return 0.0
        # proportional, clamped; slows near the target heading
        rate = max(-TURN_RATE_MAX, min(TURN_RATE_MAX, 2.0 * error))
        return rate
