#!/usr/bin/env python3
"""M2 skill evaluator: separate run-state / contract / motion-quality verdicts.

被网关放行 ≠ 动作成功。每个评价报告分列三层:
- run_state: 回放进程与步数是否符合预期;
- contract: 模型路径/命令编码是否与注册契约一致;
- motion: 按编码家族(velocity/phase/posture_flag/constant)的动作证据。

verdict ∈ passed / process_only / failed / insufficient_data。
单次固定条件下的一次通过记 level=sim_smoke_passed,不泛化。

done_count 细分(见 done_breakdown): 物理终止 / 时间截断 / 人工 reset /
取消 / 进程异常;正常 time_out 不算跌倒,但该次任务不得称无重置连续执行。

阈值按 M0-B 实测标定(velstand walk: 站立根高 ~0.117m, 地板接触 0.005m,
vx=0.15 实测 mean 0.134 m/s;ground_pick 关节方差 max 0.30)。仅对本仓库
仿真配置有效,不外推真机。
"""

from __future__ import annotations

import math
import statistics


VERDICTS = ("passed", "process_only", "failed", "insufficient_data")

# 实测标定(sim-only)
STAND_HEIGHT_MIN_M = 0.09       # 站立根高下界(实测 0.117)
FALLEN_HEIGHT_MAX_M = 0.04      # 低于此值视为倒地/地板接触(实测 0.005)
SPEED_RATIO_BAND = (0.5, 1.3)   # mean_planar_speed / |commanded|
LATERAL_DRIFT_MAX_M = 0.05      # 横向漂移绝对上限
JOINT_VAR_MOTION_MIN = 1.0e-3   # 有真实动作的关节方差下界(实测 0.30)
PHASE_COVERAGE_TOL = 0.05
TERMINAL_WINDOW = 0.2           # 末段稳定窗口比例
TERMINAL_HEIGHT_VAR_MAX = 1.0e-3
MIN_ROLLS = 0.5                 # roulade 至少约半周以上真实旋转
# kick 抬腿阈值: 以 M0-B walk 步态抬脚峰值 0.0111m 为标定样本,1.5x 下界
# (kick_left 实测左脚 0.0247 / kick_right 右脚 0.0291,均明显超出步态背景)
KICK_LIFT_MIN_M = 0.017


def _check(name: str, ok: bool | None, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def done_breakdown(summary: dict, session_results: dict | None = None) -> dict:
    """done_count 细分: 物理终止/时间截断/人工 reset/取消/进程异常."""
    breakdown = {
        "physical_termination": 0,
        "time_truncation": 0,
        "manual_reset": 0,
        "cancelled": 0,
        "process_anomaly": 0,
        "unknown": 0,
    }
    session = summary.get("session")
    if session is not None:
        if session.get("cancelled"):
            breakdown["cancelled"] += 1
        for result in (session.get("results") or {}).values():
            if result.get("status") == "interrupted_by_reset":
                reason = result.get("reason", "")
                if "time_out" in reason:
                    breakdown["time_truncation"] += 1
                else:
                    breakdown["physical_termination"] += 1
        return breakdown
    done_count = int(summary.get("done_count", 0))
    # M0 固定命令路径没有 reset 原因记录:无法细分的计入 unknown
    breakdown["unknown"] = done_count
    return breakdown


def evaluate(
    skill: str,
    record: dict,
    summary: dict | None,
    trace_rows: list[dict] | None = None,
    expected: dict | None = None,
) -> dict:
    """三层分列评价。summary 为 play 脚本输出;trace_rows 可缺省(降级)."""
    encoding = record.get("command", {}).get("encoding")
    report = {
        "skill": skill,
        "encoding": encoding,
        "run_state": None,
        "contract": None,
        "motion": None,
        "verdict": "insufficient_data",
        "level": None,
        "checks": [],
        "done_breakdown": None,
    }
    if summary is None:
        report["run_state"] = "missing"
        report["checks"].append(_check("summary_present", False, "no playback summary"))
        return report
    report["done_breakdown"] = done_breakdown(summary)

    # -- run_state ---------------------------------------------------------
    expected = expected or {}
    if "steps" in expected and summary.get("steps") != expected["steps"]:
        report["run_state"] = "failed"
        report["checks"].append(_check(
            "steps", False, f"steps {summary.get('steps')} != expected {expected['steps']}"))
        report["verdict"] = "failed"
        return report
    report["run_state"] = "completed"
    report["checks"].append(_check("steps", True, f"steps={summary.get('steps')}"))

    # -- contract ------------------------------------------------------------
    model_path = (record.get("model") or {}).get("path")
    if model_path and summary.get("checkpoint") != model_path:
        report["contract"] = "mismatch"
        report["checks"].append(_check(
            "checkpoint", False,
            f"{summary.get('checkpoint')} != registry {model_path}"))
        report["verdict"] = "failed"
        return report
    report["contract"] = "verified"
    report["checks"].append(_check("checkpoint", True, "registry model path matches"))

    # -- motion ----------------------------------------------------------------
    motion_fn = {
        "velocity": _evaluate_velocity,
        "phase": _evaluate_phase,
        "posture_flag": _evaluate_posture_flag,
        "constant": _evaluate_constant,
    }.get(encoding)
    if motion_fn is None:
        report["motion"] = "no_evaluator"
        report["verdict"] = "process_only"
        return report
    motion_verdict, checks = motion_fn(record, summary, trace_rows)
    report["checks"].extend(checks)
    report["motion"] = motion_verdict
    if motion_verdict == "failed":
        report["verdict"] = "failed"
    elif motion_verdict == "insufficient_data":
        report["verdict"] = "process_only"
    elif summary.get("done_count", 0) > 0:
        # 即使动作证据通过,发生终止/截断不得称无重置连续执行
        report["verdict"] = "process_only"
        report["checks"].append(_check(
            "done_count", False, f"done_count={summary.get('done_count')}: "
            "不得称无重置连续执行"))
    else:
        report["verdict"] = "passed"
        report["level"] = "sim_smoke_passed"  # 单次固定条件,不泛化
    return report


def _standing_ok(summary: dict) -> bool:
    return float(summary.get("min_root_height_m", 0.0)) > FALLEN_HEIGHT_MAX_M


def _evaluate_velocity(record, summary, trace_rows) -> tuple[str, list[dict]]:
    checks: list[dict] = []
    command = summary.get("command") or [0.0, 0.0, 0.0]
    vx, vy, yaw = (float(command[0]), float(command[1]), float(command[2]))
    speed = float(summary.get("mean_planar_speed_mps", 0.0))
    if not _standing_ok(summary):
        checks.append(_check("not_fallen", False,
                             f"min_root_height {summary.get('min_root_height_m')} "
                             f"<= {FALLEN_HEIGHT_MAX_M}: 倒地"))
        return "failed", checks
    checks.append(_check("not_fallen", True, "root height above fall threshold"))
    commanded = max(abs(vx), abs(vy))
    if commanded == 0.0 and abs(yaw) == 0.0:
        ok = speed < 0.05
        checks.append(_check("stand_hold", ok, f"mean speed {speed:.4f} m/s (zero command)"))
        return ("passed" if ok else "failed"), checks
    if vx != 0.0:
        forward = float(summary.get("mean_forward_displacement_m", 0.0))
        sign_ok = math.copysign(1.0, forward) == math.copysign(1.0, vx) and abs(forward) > 0.01
        checks.append(_check("direction", sign_ok,
                             f"forward displacement {forward:.4f} vs vx={vx}"))
        ratio = speed / abs(vx)
        ratio_ok = SPEED_RATIO_BAND[0] <= ratio <= SPEED_RATIO_BAND[1]
        checks.append(_check("speed_ratio", ratio_ok, f"ratio {ratio:.3f}"))
        lateral = float(summary.get("mean_abs_lateral_displacement_m", 0.0))
        lat_ok = lateral <= max(LATERAL_DRIFT_MAX_M, 0.5 * abs(forward))
        checks.append(_check("lateral_drift", lat_ok, f"abs lateral {lateral:.4f} m"))
        if all(c["ok"] for c in checks):
            return "passed", checks
        return "failed", checks
    # vy/yaw-only commands: direction evidence需要 trace 航向,保守降级
    checks.append(_check("direction", None, "vy/yaw command: heading evidence needs trace"))
    return "insufficient_data", checks


def _unwrap_phase(trace_rows: list[dict]) -> list[float]:
    phases = [float(row["dance_phase"]) for row in trace_rows]
    unwrapped = [phases[0]]
    cycles = 0.0
    for prev, cur in zip(phases, phases[1:]):
        if cur < prev - 0.5:  # 模 1 回绕:记录周期编号而非以单调为通用标准
            cycles += 1.0
        unwrapped.append(cur + cycles)
    return unwrapped


def _evaluate_phase(record, summary, trace_rows) -> tuple[str, list[dict]]:
    checks: list[dict] = []
    if not trace_rows or trace_rows[0].get("dance_phase") is None:
        checks.append(_check("trace", None, "phase evaluation needs the trace"))
        return "insufficient_data", checks
    command = record.get("command", {})
    end_phase = float(command.get("end_phase", 1.0))
    unwrapped = _unwrap_phase(trace_rows)
    coverage = unwrapped[-1] - unwrapped[0]
    cov_ok = coverage >= end_phase - PHASE_COVERAGE_TOL
    checks.append(_check("phase_coverage", cov_ok,
                         f"unwrapped coverage {coverage:.3f} vs end_phase {end_phase}"))
    joint_series = [row["joint_position"][0] for row in trace_rows]
    max_var = max(statistics.pvariance(col) for col in zip(*joint_series))
    motion_ok = max_var > JOINT_VAR_MOTION_MIN
    checks.append(_check("motion_present", motion_ok,
                         f"max joint variance {max_var:.2e}"))
    if not _standing_ok(summary):
        checks.append(_check("not_fallen", False, "root height at floor level"))
    else:
        checks.append(_check("not_fallen", True, "root height above fall threshold"))
    if all(c["ok"] for c in checks):
        return "passed", checks
    return "failed", checks


def _height_series(trace_rows: list[dict]) -> list[float]:
    return [float(row["root_position"][0][2]) for row in trace_rows]


def _evaluate_posture_flag(record, summary, trace_rows) -> tuple[str, list[dict]]:
    checks: list[dict] = []
    if not trace_rows:
        checks.append(_check("trace", None, "posture evaluation needs the trace"))
        return "insufficient_data", checks
    posture = record.get("posture")
    heights = _height_series(trace_rows)
    window = max(1, int(len(heights) * TERMINAL_WINDOW))
    start_mean = statistics.fmean(heights[:window])
    end_mean = statistics.fmean(heights[-window:])
    if max(heights) < FALLEN_HEIGHT_MAX_M:
        # 全程地板高度:趴倒静止不算坐下/站立
        checks.append(_check("not_lying_flat", False,
                             f"height never leaves floor (max {max(heights):.3f})"))
        return "failed", checks
    checks.append(_check("not_lying_flat", True, "left floor level during motion"))
    if posture == "sit":
        direction_ok = end_mean < start_mean - 0.01
    else:  # stand
        direction_ok = end_mean > start_mean + 0.01
    checks.append(_check("direction", direction_ok,
                         f"{start_mean:.3f} -> {end_mean:.3f} ({posture})"))
    terminal_var = statistics.pvariance(heights[-window:]) if window > 1 else 0.0
    stable_ok = terminal_var < TERMINAL_HEIGHT_VAR_MAX
    checks.append(_check("stable_terminal", stable_ok,
                         f"terminal height variance {terminal_var:.2e}"))
    if posture == "stand":
        height_ok = end_mean > STAND_HEIGHT_MIN_M
        checks.append(_check("stand_height", height_ok, f"terminal {end_mean:.3f} m"))
    if all(c["ok"] for c in checks):
        return "passed", checks
    return "failed", checks


def _evaluate_constant(record, summary, trace_rows) -> tuple[str, list[dict]]:
    checks: list[dict] = []
    skill_has_roll = "roulade" in str(record.get("manifest_entry", {}).get("file", "")) or \
        "roulade" in str(record.get("model", {}).get("path", ""))
    if skill_has_roll:
        rolls = float(summary.get("mean_forward_rolls", 0.0)) + float(
            summary.get("mean_reverse_rolls", 0.0))
        # 仅末帧站立不算翻滚:必须有真实旋转过程
        roll_ok = rolls >= MIN_ROLLS
        checks.append(_check("rotation_process", roll_ok,
                             f"total rolls {rolls:.3f} (forward+reverse)"))
        return ("passed" if roll_ok else "failed"), checks
    # kick 等:需要 trace 中指定腿抬升证据(另一只脚不得同步抬起)
    if not trace_rows:
        checks.append(_check("trace", None, "kick evaluation needs the trace"))
        return "insufficient_data", checks
    model_file = str(record.get("model", {}).get("path", ""))
    leg_index = 0 if "left" in model_file else 1  # velocity_trace: 0=left 1=right
    leg_name = "left" if leg_index == 0 else "right"
    kick_peak = 0.0
    other_peak = 0.0
    for row in trace_rows:
        for env_feet in row.get("foot_position", []):
            if len(env_feet) != 2:
                continue
            kick_peak = max(kick_peak, float(env_feet[leg_index][2]))
            other_peak = max(other_peak, float(env_feet[1 - leg_index][2]))
    kick_ok = kick_peak > KICK_LIFT_MIN_M and other_peak < KICK_LIFT_MIN_M
    checks.append(_check(
        "leg_lift", kick_ok,
        f"{leg_name} foot peak {kick_peak:.4f} m (threshold {KICK_LIFT_MIN_M}, "
        f"calibrated 1.5x walk-gait 0.0111), other foot {other_peak:.4f} m"))
    return ("passed" if kick_ok else "failed"), checks
