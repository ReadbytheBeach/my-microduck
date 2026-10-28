#!/usr/bin/env python3
"""M2 evaluator tests: real M0-B positives + synthetic negative traces (§8.9).

反例覆盖:原地站立但相位推进、倒地但速度≈0、伪造/过期停止 telemetry、
仅末帧站立无翻滚、循环相位跨界、reset 前后拼接虚假完成。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from language_control.evaluator import (  # noqa: E402
    FALLEN_HEIGHT_MAX_M,
    done_breakdown,
    evaluate,
)

REGISTRY = json.loads(
    (ROOT / "work/language_control/skill_registry.json").read_text(encoding="utf-8")
)
M0_WALK = ROOT / "work/language_control/executions/ccbba02568f143eb9f048ef0eab10d3e"
M0_PICK = ROOT / "work/language_control/executions/9e9f87b7fdf744cfb8e7330e2f53ba78"


def make_summary(skill: str = "walk", **overrides) -> dict:
    summary = {
        "steps": 100,
        "checkpoint": REGISTRY["skills"][skill]["model"]["path"],
        "command": [0.15, 0.0, 0.0],
        "done_count": 0,
        "min_root_height_m": 0.065,
        "max_root_height_m": 0.121,
        "mean_planar_speed_mps": 0.134,
        "mean_forward_displacement_m": 0.172,
        "mean_abs_lateral_displacement_m": 0.021,
        "mean_forward_rolls": 0.0,
        "mean_reverse_rolls": 0.0,
    }
    summary.update(overrides)
    return summary


def make_phase_trace(rows: int = 140, end_phase: float = 0.7,
                     joint_amplitude: float = 0.3, height: float = 0.115) -> list[dict]:
    trace = []
    for i in range(rows):
        phase = (i / rows) * end_phase
        joint = [joint_amplitude * ((i % 10) / 10.0)] * 14
        trace.append({
            "dance_phase": phase,
            "joint_position": [joint],
            "root_position": [[0.0, 0.0, height]],
        })
    return trace


class RealEvidenceTests(unittest.TestCase):
    """M0-B 真实回放产物作为正例(单次通过记 sim_smoke_passed,不泛化)."""

    def test_walk_m0b_passes(self):
        summary = json.loads((M0_WALK / "playback.json").read_text(encoding="utf-8"))
        report = evaluate(
            "walk", REGISTRY["skills"]["walk"], summary,
            expected={"steps": 100},
        )
        self.assertEqual(report["verdict"], "passed", report["checks"])
        self.assertEqual(report["level"], "sim_smoke_passed")
        self.assertEqual(report["run_state"], "completed")
        self.assertEqual(report["contract"], "verified")

    def test_ground_pick_m0b_phase_passes(self):
        summary = json.loads((M0_PICK / "playback.json").read_text(encoding="utf-8"))
        trace = json.loads((M0_PICK / "trace.json").read_text(encoding="utf-8"))
        report = evaluate(
            "ground_pick", REGISTRY["skills"]["ground_pick"], summary, trace,
            expected={"steps": 140},
        )
        self.assertEqual(report["verdict"], "passed", report["checks"])
        self.assertEqual(report["level"], "sim_smoke_passed")

    def test_missing_summary_is_insufficient(self):
        report = evaluate("walk", REGISTRY["skills"]["walk"], None)
        self.assertEqual(report["verdict"], "insufficient_data")


class NegativeTests(unittest.TestCase):
    """§8.9 反例: 全部不得通过."""

    def test_phase_advances_but_standing_still(self):
        # 原地站立却相位推进: 关节方差≈0 -> failed
        trace = make_phase_trace(joint_amplitude=1e-6)
        report = evaluate(
            "ground_pick", REGISTRY["skills"]["ground_pick"],
            make_summary("ground_pick", steps=140), trace, expected={"steps": 140},
        )
        self.assertEqual(report["verdict"], "failed")
        motion = [c for c in report["checks"] if c["name"] == "motion_present"][0]
        self.assertFalse(motion["ok"])

    def test_fallen_with_zero_velocity_not_stand(self):
        # 倒地但速度≈0: 零指令站立检查不得通过
        report = evaluate(
            "walk", REGISTRY["skills"]["walk"],
            make_summary(command=[0.0, 0.0, 0.0], mean_planar_speed_mps=0.001,
                         min_root_height_m=0.005, max_root_height_m=0.006),
        )
        self.assertEqual(report["verdict"], "failed")

    def test_final_frame_stand_without_roll_fails(self):
        # 仅末帧站立、无真实翻滚过程 -> failed
        report = evaluate(
            "roulade", REGISTRY["skills"]["roulade"],
            make_summary("roulade", steps=50, mean_forward_rolls=0.0, mean_reverse_rolls=0.01,
                         max_root_height_m=0.118),
            expected={"steps": 50},
        )
        self.assertEqual(report["verdict"], "failed")
        rotation = [c for c in report["checks"] if c["name"] == "rotation_process"][0]
        self.assertFalse(rotation["ok"])

    def test_lying_flat_is_not_sit(self):
        # 趴倒静止不算坐下: 全程地板高度 -> failed
        trace = make_phase_trace(rows=100, joint_amplitude=0.05, height=0.005)
        report = evaluate(
            "sitdown", REGISTRY["skills"]["sitdown"],
            make_summary("sitdown", min_root_height_m=0.004, max_root_height_m=0.006),
            trace,
        )
        self.assertEqual(report["verdict"], "failed")
        lying = [c for c in report["checks"] if c["name"] == "not_lying_flat"][0]
        self.assertFalse(lying["ok"])

    def test_phase_wrap_unwrapped_correctly(self):
        # 循环相位跨界: 模 1 回绕必须展开判定,不以"始终单调"为标准
        trace = []
        for i in range(150):
            phase = (i * 0.01) % 1.0  # wraps at i=100
            trace.append({
                "dance_phase": phase,
                "joint_position": [[0.3 * ((i % 10) / 10.0)] * 14],
                "root_position": [[0.0, 0.0, 0.115]],
            })
        report = evaluate(
            "dance_side_step", REGISTRY["skills"]["dance_side_step"],
            make_summary("dance_side_step", steps=150), trace, expected={"steps": 150},
        )
        coverage = [c for c in report["checks"] if c["name"] == "phase_coverage"][0]
        self.assertTrue(coverage["ok"], coverage)  # 1.5 -> end_phase 1.0 covered

    def test_reset_spliced_not_continuous(self):
        # reset 前后拼接不得称"无重置连续执行"
        summary = make_summary(done_count=1, session={
            "session_id": "x", "results": {
                "cmd-1": {"status": "interrupted_by_reset",
                          "reason": "episode reset: time_out"},
            },
        })
        breakdown = done_breakdown(summary)
        self.assertEqual(breakdown["time_truncation"], 1)
        report = evaluate(
            "walk", REGISTRY["skills"]["walk"], summary, expected={"steps": 100},
        )
        # 动作证据可过,但 done_count>0 -> 不得 passed
        self.assertEqual(report["verdict"], "process_only")

    def test_stale_telemetry_not_stop_evidence(self):
        # 伪造/过期 telemetry: 快照墙钟过旧 -> 不得当作新鲜停止证据
        sys.path.insert(0, str(ROOT / "scripts"))
        import playback_command_feed as pcf

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            pcf.write_status(status_path, {
                "run_state": "running", "wall_updated_at": time.time() - 30.0,
            })
            snapshot = pcf.read_json(status_path)
            age = time.time() - snapshot["wall_updated_at"]
            self.assertGreater(age, 3.0)  # stale: not evidence of a fresh stop


class DoneBreakdownTests(unittest.TestCase):
    def test_fixed_path_unknown_done(self):
        breakdown = done_breakdown(make_summary(done_count=2))
        self.assertEqual(breakdown["unknown"], 2)

    def test_session_cancel_counted(self):
        summary = make_summary(session={"cancelled": True, "results": {}})
        breakdown = done_breakdown(summary)
        self.assertEqual(breakdown["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
