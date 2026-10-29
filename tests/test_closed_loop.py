#!/usr/bin/env python3
"""Closed-loop move_distance / turn tests (§3.6) — unit + stub session e2e."""

from __future__ import annotations

import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from language_control import closed_loop as cl  # noqa: E402
from language_control.intent import SequenceParser  # noqa: E402
from language_control.proposal import ProposalError  # noqa: E402
from test_session_service import StubSession  # noqa: E402


class DistanceControllerUnitTests(unittest.TestCase):
    def drive(self, distance, steps=2000, dt=0.02, drift_per_step=0.0):
        controller = cl.DistanceController(distance)
        x = y = 0.0
        yaw = 0.0
        velocities = []
        for i in range(steps):
            vx, yaw_rate = controller.update((x, y), yaw, i * dt)
            velocities.append(vx)
            if controller.status != "running":
                break
            yaw += yaw_rate * dt + drift_per_step
            # 机体前向 = 测量 yaw + 资产根坐标系偏移(与真实 Isaac 一致)
            heading = yaw + cl.YAW_OFFSET_RAD
            x += vx * math.cos(heading) * dt
            y += vx * math.sin(heading) * dt
        return controller, velocities, (x, y)

    def test_converges_with_two_phase_speed(self):
        controller, velocities, _pos = self.drive(0.3)
        self.assertEqual(controller.status, cl.DONE)
        final = controller.final
        self.assertLess(abs(final["endpoint_error_m"]), cl.PROGRESS_TOLERANCE_M + 0.01)
        self.assertEqual(final["position_source"], "sim_ground_truth")
        # 全程巡航(中途降速会失速,见手册修复记录);到达即停
        self.assertIn(cl.CRUISE_VX, velocities)
        self.assertEqual(velocities[-2], cl.CRUISE_VX)  # 末拍为停止零速

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            cl.DistanceController(0.01)
        with self.assertRaises(ValueError):
            cl.DistanceController(5.0)

    def test_lateral_fault(self):
        controller = cl.DistanceController(0.5)
        controller.update((0.0, 0.0), 0.0, 0.0)
        # 横向跑偏: 位置垂直于起始朝向偏移超限
        controller.update((0.05, 0.2), 0.0, 0.5)
        self.assertEqual(controller.status, cl.LATERAL_FAULT)

    def test_heading_fault(self):
        controller = cl.DistanceController(0.5)
        controller.update((0.0, 0.0), 0.0, 0.0)
        controller.update((0.05, 0.0), math.radians(45.0), 0.5)
        self.assertEqual(controller.status, cl.HEADING_FAULT)

    def test_timeout(self):
        controller = cl.DistanceController(0.5)
        controller.update((0.0, 0.0), 0.0, 0.0)
        controller.update((0.01, 0.0), 0.0, 10_000.0)  # 仿真时间远超预算
        self.assertEqual(controller.status, cl.TIMEOUT)


class TurnControllerUnitTests(unittest.TestCase):
    def drive(self, deg, steps=3000, dt=0.02):
        controller = cl.TurnController(deg)
        yaw = 0.0
        for i in range(steps):
            rate = controller.update(yaw, i * dt)
            if controller.status != "running":
                break
            yaw += rate * dt
        return controller, yaw

    def test_left_turn_30_deg(self):
        controller, yaw = self.drive(30.0)
        self.assertEqual(controller.status, cl.DONE)
        self.assertAlmostEqual(math.degrees(yaw), 30.0, delta=2.0)
        self.assertLess(abs(controller.final["heading_error_deg"]), 2.0)

    def test_right_turn(self):
        controller, yaw = self.drive(-45.0)
        self.assertEqual(controller.status, cl.DONE)
        self.assertAlmostEqual(math.degrees(yaw), -45.0, delta=2.0)

    def test_angle_wrap(self):
        # 目标 170°,起始朝向近 -pi: 回绕路径不得走反
        controller = cl.TurnController(170.0)
        yaw = math.pi - 0.01
        controller.update(yaw, 0.0)
        turned = cl.wrap_angle(yaw - controller.yaw0)
        self.assertAlmostEqual(turned, 0.0, places=6)
        # 越过 pi 边界继续转
        for i in range(1, 3000):
            rate = controller.update(yaw, i * 0.02)
            if controller.status != "running":
                break
            yaw = cl.wrap_angle(yaw + rate * 0.02)
        self.assertEqual(controller.status, cl.DONE)
        self.assertLess(abs(controller.final["heading_error_deg"]), 2.0)


class IntentGoalTests(unittest.TestCase):
    def setUp(self):
        self.parser = SequenceParser()

    def test_distance_phrases(self):
        self.assertEqual(
            self.parser.parse("向前走30厘米").steps[0].params, {"distance_m": 0.3})
        self.assertEqual(
            self.parser.parse("走0.5米").steps[0].params, {"distance_m": 0.5})
        self.assertEqual(
            self.parser.parse("走三百毫米").steps[0].params, {"distance_m": 0.3})

    def test_turn_phrases(self):
        self.assertEqual(
            self.parser.parse("左转30度").steps[0].params, {"turn_deg": 30.0})
        self.assertEqual(
            self.parser.parse("右转四十五度").steps[0].params, {"turn_deg": -45.0})

    def test_minutes_still_capability_gap(self):
        with self.assertRaises(ProposalError) as ctx:
            self.parser.parse("走两分钟")
        self.assertEqual(ctx.exception.classification, "capability_gap")

    def test_sequence_with_goal(self):
        proposal = self.parser.parse("向前走30厘米，然后停下")
        self.assertEqual([s.skill for s in proposal.steps], ["walk", "stop"])
        self.assertEqual(proposal.steps[0].params, {"distance_m": 0.3})


class ClosedLoopStubSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_move_distance_via_stub(self):
        with StubSession(self.tmp_path, name="cl", streaming_lease_s=3600.0,
                           stub_options={"tick_sleep": 0.01}) as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"distance_m": 0.3}}],
            }, timeout=30.0)
            self.assertEqual(response["status"], "completed")
            final = response["result"]
            self.assertEqual(final["status"], "done")
            self.assertLess(abs(final["endpoint_error_m"]), 0.02)
            self.assertEqual(final["position_source"], "sim_ground_truth")
            self.assertAlmostEqual(final["progress_m"], 0.3, delta=0.02)

    def test_turn_via_stub(self):
        with StubSession(self.tmp_path, name="turn", streaming_lease_s=3600.0,
                           stub_options={"tick_sleep": 0.01}) as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"turn_deg": 30.0}}],
            }, timeout=30.0)
            self.assertEqual(response["status"], "completed")
            final = response["result"]
            self.assertAlmostEqual(final["turned_deg"], 30.0, delta=2.5)

    def test_out_of_range_goal_rejected_not_estimated(self):
        with StubSession(self.tmp_path, name="range") as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"distance_m": 5.0}}],
            })
            self.assertEqual(response["status"], "rejected")
            self.assertEqual(response["classification"], "capability_gap")

    def test_frozen_body_timeout_returns_terminal_response(self):
        """吸引子场景：指令已应用但机体不动 → 闭环超时必须返回 failed，
        不得让等待方永远挂起（回归：closed_loop_end 失败分支曾先清
        _active，导致响应永远不写）。"""
        with StubSession(self.tmp_path, name="frz", streaming_lease_s=3600.0,
                         stub_options={"tick_sleep": 0.01, "freeze": 1}) as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"distance_m": 0.3}}],
            }, timeout=60.0)
            self.assertEqual(response["status"], "failed")
            self.assertIn("closed-loop", response["reason"])


if __name__ == "__main__":
    unittest.main()
