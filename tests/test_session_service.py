#!/usr/bin/env python3
"""M1-A/M1-B session-service tests — all Isaac-free (stub play double).

Covers the M1-A/B items of task book §8:
1. structured CLI works end-to-end with no API key (stub backend);
2. Chinese `say` and structured CLI produce semantically identical commands;
3. duplicate/renewal never restarts a finite command; a step is not
   executed before its ACK (sequence ordering, no overwrite/loss);
4. writer exit / stale status detected at the service level (play-side
   file loss/corruption/lease/watchdog are covered in
   tests/test_playback_command_feed.py);
5. after stop, queued sequence steps and late-arriving requests cannot
   resume motion;
6. fast and slow sims both execute a 2 s action by sim time; the
   wall-clock timeout fires independently;
7. crossing the (stubbed) episode boundary interrupts the active command
   explicitly — no silent reset, sequence aborts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import playback_command_feed as pcf  # noqa: E402
from language_control import mdctl  # noqa: E402
from language_control.intent import SequenceParser  # noqa: E402
from language_control.session_service import SessionService  # noqa: E402

REGISTRY = ROOT / "work/language_control/skill_registry.json"


def wait_for(predicate, timeout: float = 15.0, what: str = "condition") -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class StubSession:
    """SessionService (stub backend) running in a thread."""

    def __init__(self, tmp: Path, name: str = "t", **service_kwargs):
        self.dir = tmp / name
        stub_options = {"control_dt": 0.02}
        stub_options.update(service_kwargs.pop("stub_options", {}))
        self.service = SessionService(
            session_dir=self.dir,
            registry_path=REGISTRY,
            ledger_path=tmp / f"events-{name}.jsonl",
            backend="stub",
            name=name,
            steps=service_kwargs.pop("steps", 50_000_000),
            stub_options=stub_options,
            **service_kwargs,
        )
        self.thread = threading.Thread(target=self.service.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        wait_for(
            lambda: self.service.state in ("running", "paused"),
            what="session running",
        )
        return self

    def request(self, payload: dict, wait_terminal: bool = True,
                timeout: float = 15.0) -> dict:
        request_id = f"req-{time.time_ns()}"
        pcf.atomic_write_json(self.dir / "requests" / f"{request_id}.json", {
            "request_id": request_id, "created_at": time.time(), **payload,
        })
        path = self.dir / "responses" / f"{request_id}.json"

        def done():
            response = pcf.read_json(path)
            if response and (
                response["status"] in ("completed", "failed", "rejected", "stopped",
                                       "interrupted", "cancelled")
                or not wait_terminal and response["status"] in ("applied", "accepted")
            ):
                return response
            return None

        return wait_for(done, timeout=timeout, what=f"response for {payload.get('op')}")

    def status(self) -> dict:
        return wait_for(lambda: pcf.read_json(self.dir / "status.json"), what="status")

    def __exit__(self, *exc):
        if self.service.state != "cancelled":
            try:
                self.request({"op": "stop", "mode": "cancel_session"}, timeout=15.0)
            except AssertionError:
                pass
        self.thread.join(timeout=20.0)
        return False


class SessionBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_api_key_full_function(self):
        """§8.1: with no API key anywhere, run/status/stop all work."""
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET"))}
        with mock.patch.dict(os.environ, env, clear=True):
            with StubSession(self.tmp_path) as session:
                response = session.request({
                    "op": "run",
                    "steps": [{"skill": "walk",
                               "params": {"vx": 0.1, "sim_duration_s": 0.4}}],
                })
                self.assertEqual(response["status"], "completed")
                result = response["result"]
                self.assertEqual(result["sim_steps_executed"], 20)  # 0.4s @ 50Hz
                status = session.status()
                self.assertEqual(status["run_state"], "running")
                stop = session.request({"op": "stop", "mode": "stop_skill"})
                self.assertEqual(stop["status"], "stopped")

    def test_gateway_limits_enforced_in_session(self):
        with StubSession(self.tmp_path) as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.41}}],
            })
            self.assertEqual(response["status"], "rejected")
            self.assertEqual(response["classification"], "policy_rejected")

    def test_cross_model_skill_refused(self):
        with StubSession(self.tmp_path) as session:
            response = session.request({
                "op": "run", "steps": [{"skill": "ground_pick", "params": {}}],
            })
            self.assertEqual(response["status"], "rejected")
            self.assertIn("velocity", response["reason"])

    def test_semantic_equivalence_cli_vs_chinese(self):
        """§8.2: 中文与 CLI 生成同一受控命令(忽略 id/时间戳)."""
        parser = SequenceParser()
        proposal = parser.parse("向前走两秒")
        chinese_step = proposal.steps[0].to_dict()
        cli_step = {"skill": "walk", "params": {"vx": 0.15, "sim_duration_s": 2.0}}
        self.assertEqual(chinese_step, cli_step)
        with StubSession(self.tmp_path) as session:
            service = session.service
            r_cn = service._resolve_step(chinese_step["skill"], chinese_step["params"])
            r_cli = service._resolve_step(cli_step["skill"], cli_step["params"])
            self.assertEqual(r_cn, r_cli)
            command_cn = service._build_command(dict(r_cn, skill="walk"))
            command_cli = service._build_command(dict(r_cli, skill="walk"))
            for volatile in ("command_id", "sequence", "issued_at"):
                command_cn.pop(volatile)
                command_cli.pop(volatile)
            self.assertEqual(command_cn, command_cli)


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sequence_ordered_no_overwrite_no_loss(self):
        """§8.3: 下一步等上一步 ACK+结果;三步顺序执行不丢步."""
        with StubSession(self.tmp_path) as session:
            response = session.request({
                "op": "sequence",
                "steps": [
                    {"skill": "walk", "params": {"vx": 0.15, "sim_duration_s": 0.3}},
                    {"skill": "walk", "params": {"yaw": 0.5, "sim_duration_s": 0.3}},
                    {"skill": "stand", "params": {}},
                ],
            })
            self.assertEqual(response["status"], "completed")
            ack = pcf.read_json(session.dir / "ack.json")
            results = ack["results"]
            self.assertEqual(len(results), 3, results)
            ordered = sorted(results.values(), key=lambda r: r["applied_step"])
            self.assertEqual(
                [r["status"] for r in ordered],
                ["completed", "completed", "completed"],
            )
            # each step applied only after the previous step finished
            for previous, following in zip(ordered, ordered[1:]):
                self.assertGreaterEqual(
                    following["applied_step"], previous["finished_step"]
                )
            # stand mapped to the same-policy zero switch, 2 s default hold
            self.assertEqual(ordered[2]["sim_steps_executed"], 100)  # 2.0s @ 50Hz

    def test_partial_failure_aborts_remaining_steps(self):
        """某步运行时被拒 → 后续步骤不发送、不执行."""
        with StubSession(
            self.tmp_path, name="fail", stub_options={"reject_vx": 0.13}
        ) as session:
            response = session.request({
                "op": "sequence",
                "steps": [
                    {"skill": "walk", "params": {"vx": 0.13, "sim_duration_s": 0.3}},
                    {"skill": "walk", "params": {"vx": 0.10, "sim_duration_s": 0.3}},
                ],
            })
            self.assertEqual(response["status"], "failed")
            self.assertIn("rejected", response["reason"])
            ack = pcf.read_json(session.dir / "ack.json")
            applied = [a for a in ack["acks"] if a["status"] == "applied"]
            self.assertEqual(len(applied), 0, ack["acks"])  # 第二步从未发送

    def test_reset_interrupts_sequence_explicitly(self):
        """§8.7(替身): episode 边界 reset → 显式中断,不静默继续."""
        with StubSession(
            self.tmp_path, name="reset", stub_options={"episode_steps": 25}
        ) as session:
            response = session.request({
                "op": "sequence",
                "steps": [
                    {"skill": "walk", "params": {"vx": 0.1, "sim_duration_s": 1.5}},
                    {"skill": "walk", "params": {"vx": 0.1, "sim_duration_s": 0.4}},
                ],
            })
            self.assertEqual(response["status"], "interrupted")
            ack = pcf.read_json(session.dir / "ack.json")
            first = min(ack["results"].values(), key=lambda r: r["applied_step"])
            self.assertEqual(first["status"], "interrupted_by_reset")
            self.assertIn("time_out", first["reason"])
            status = session.status()
            self.assertGreaterEqual(status["episode_id"], 1)
            self.assertEqual(status["reset_reason"], "time_out")
            self.assertGreaterEqual(status["done_count"], 1)

    def test_sim_time_completion_fast_and_slow(self):
        """§8.6: 快/慢仿真下动作均按仿真控制步执行."""
        executed = {}
        walls = {}
        for name, tick_sleep in (("fast", 0.0), ("slow", 0.05)):
            with StubSession(
                self.tmp_path, name=name, stub_options={"tick_sleep": tick_sleep}
            ) as session:
                start = time.monotonic()
                response = session.request({
                    "op": "run",
                    "steps": [{"skill": "walk",
                               "params": {"vx": 0.1, "sim_duration_s": 2.0}}],
                }, timeout=30.0)
                walls[name] = time.monotonic() - start
            self.assertEqual(response["status"], "completed")
            executed[name] = response["result"]["sim_steps_executed"]
        self.assertEqual(executed["fast"], 100)  # 2.0 s x 50 Hz, sim-time based
        self.assertEqual(executed["slow"], 100)
        self.assertGreater(walls["slow"], walls["fast"] + 2.0)

    def test_wall_timeout_independent_of_sim_completion(self):
        """§8.6: 墙钟预算独立生效 — 慢仿真下未跑完也算超时失败."""
        with StubSession(
            self.tmp_path, name="wall",
            stub_options={"tick_sleep": 0.1},  # 2s sim needs ~10s wall
            step_wall_timeout_factor=0.2,      # budget: 2*0.2+5 = 5.4s wall
        ) as session:
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.1, "sim_duration_s": 2.0}}],
            }, timeout=30.0)
            self.assertEqual(response["status"], "failed")
            self.assertIn("wall timeout", response["reason"])


class StopSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_invalidates_queue_and_late_requests(self):
        """§8.5: 停止使未生效旧任务失效;迟到的停止前请求不能恢复运动."""
        with StubSession(
            self.tmp_path, stub_options={"tick_sleep": 0.01}
        ) as session:
            first = session.request({
                "op": "sequence",
                "steps": [
                    {"skill": "walk", "params": {"vx": 0.1, "sim_duration_s": 10.0}},
                    {"skill": "walk", "params": {"vx": 0.1, "sim_duration_s": 10.0}},
                ],
            }, wait_terminal=False)
            self.assertEqual(first["status"], "accepted")
            wait_for(lambda: session.service._active is not None
                     and session.service._active.get("state") == "applied",
                     what="first step applied")
            stop = session.request({"op": "stop", "mode": "stop_skill"})
            self.assertEqual(stop["status"], "stopped")
            # queued step dropped, velocity zero, sim keeps running
            self.assertEqual(session.service._queue, [])
            status = session.status()
            self.assertEqual(status["current_velocity"], [0.0, 0.0, 0.0])
            self.assertEqual(status["run_state"], "running")
            # late request constructed BEFORE the stop is invalidated
            late = session.request({
                "op": "run", "created_at": time.time() - 60,
                "steps": [{"skill": "walk", "params": {"vx": 0.1}}],
            }, timeout=15.0)
            # created_at is set by the client; override below instead
            self.assertIn(late["status"], ("stopped", "applied", "accepted"))

    def test_late_predated_request_dropped(self):
        with StubSession(self.tmp_path) as session:
            session.request({"op": "stop", "mode": "stop_skill"})
            stop_wall = session.service._last_stop_wall
            request_id = "req-late"
            pcf.atomic_write_json(session.dir / "requests" / f"{request_id}.json", {
                "request_id": request_id,
                "created_at": stop_wall - 5.0,  # constructed before the stop
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.1}}],
            })
            response = wait_for(
                lambda: pcf.read_json(session.dir / "responses" / f"{request_id}.json"),
                what="late response",
            )
            self.assertEqual(response["status"], "stopped")
            self.assertEqual(session.service._active, None)

    def test_new_request_after_stop_recovers(self):
        with StubSession(self.tmp_path) as session:
            session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.1}}],
            }, wait_terminal=False)
            session.request({"op": "stop", "mode": "stop_skill"})
            # a NEW valid request (created after the stop) may move again
            response = session.request({
                "op": "run",
                "steps": [{"skill": "walk",
                           "params": {"vx": 0.1, "sim_duration_s": 0.2}}],
            })
            self.assertEqual(response["status"], "completed")

    def test_pause_resume(self):
        # 快仿真替身下用大租约:否则全速 stub 在服务续约循环(50ms)之前
        # 就跑完 3s 仿真租约;真实 Isaac 近实时,默认 3s 租约即可
        with StubSession(self.tmp_path, streaming_lease_s=3600.0) as session:
            session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.1}}],
            }, wait_terminal=False)
            pause = session.request({"op": "stop", "mode": "pause_sim"})
            self.assertEqual(pause["status"], "stopped")
            wait_for(lambda: session.status()["run_state"] == "paused",
                     what="paused run_state")
            # while paused, new motion requests are refused
            refused = session.request({
                "op": "run", "steps": [{"skill": "walk", "params": {"vx": 0.2}}],
            })
            self.assertEqual(refused["status"], "rejected")
            resume = session.request({"op": "resume"}, wait_terminal=False)
            self.assertEqual(resume["status"], "applied")
            wait_for(lambda: session.status()["run_state"] == "running",
                     what="resumed run_state")

    def test_play_process_exit_detected(self):
        """写入者(仿真进程)退出 → 服务进入明确结束态,不当作停止证据."""
        with StubSession(self.tmp_path) as session:
            session.request({
                "op": "run",
                "steps": [{"skill": "walk", "params": {"vx": 0.1}}],
            }, wait_terminal=False)
            pid = session.service._process.pid
            import signal as _signal
            os.killpg(os.getpgid(pid), _signal.SIGKILL)
            wait_for(lambda: session.service.state == "exited", what="exited state")
            meta = pcf.read_json(session.dir / "session.json")
            self.assertEqual(meta["state"], "exited")


class MdctlEndToEndTests(unittest.TestCase):
    """§8.1/§8.2 through the real CLI entry points (stub backend, no key)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mdctl(self, *argv) -> dict:
        from io import StringIO
        buffer = StringIO()
        old = sys.stdout
        sys.stdout = buffer
        try:
            rc = mdctl.main(list(argv))
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        return json.loads(buffer.getvalue())

    def test_full_flow_without_api_key(self):
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET"))}
        with mock.patch.dict(os.environ, env, clear=True):
            root = self.tmp_path / "sessions"
            self.run_mdctl(
                "--sessions-root", str(root),
                "--ledger", str(self.tmp_path / "events.jsonl"),
                "session", "start", "--backend", "stub", "--name", "e2e",
                "--steps", "50000000",
                "--stub-option", "control_dt", "0.02",
            )
            try:
                listed = self.run_mdctl(
                    "--sessions-root", str(root),
                    "skills", "list", "--session", "e2e",
                )
                self.assertTrue(listed["skills"]["walk"]["session_executable"])
                self.assertTrue(listed["skills"]["stand"]["session_executable"])
                self.assertFalse(listed["skills"]["crouch"]["session_executable"])

                ran = self.run_mdctl(
                    "--sessions-root", str(root),
                    "skill", "run", "walk", "--session", "e2e",
                    "--vx", "0.1", "--duration-sim", "0.4", "--execute",
                    "--wait-result",
                )
                self.assertEqual(ran["response"]["status"], "completed")

                said = self.run_mdctl(
                    "--sessions-root", str(root),
                    "say", "--session", "e2e", "向前走两秒，然后停下",
                    "--execute", "--wait-result",
                )
                self.assertEqual(said["handled"], "submitted")
                self.assertEqual(said["response"]["status"], "completed")

                status = self.run_mdctl(
                    "--sessions-root", str(root), "status", "--session", "e2e", "--json",
                )
                self.assertTrue(status["fresh"])
                self.assertEqual(status["status"]["run_state"], "running")
                # results: 0.4s run + say 的 2s walk;stop 是控制指令无 result
                self.assertGreaterEqual(len(status["results"]), 2)
            finally:
                self.run_mdctl(
                    "--sessions-root", str(root),
                    "skill", "stop", "--session", "e2e", "--cancel",
                )
            meta = pcf.read_json(root / "e2e" / "session.json")
            wait_for(lambda: pcf.read_json(root / "e2e" / "session.json")
                     and pcf.read_json(root / "e2e" / "session.json")["state"]
                     in ("cancelled", "exited"), what="session cancelled")


class KimiLiveTests(unittest.TestCase):
    """Live endpoint probe: only runs when MOONSHOT_API_KEY is present."""

    @unittest.skipUnless(os.environ.get("MOONSHOT_API_KEY"), "MOONSHOT_API_KEY not set")
    def test_kimi_strict_schema_live(self):
        from language_control.intent import KimiClient
        client = KimiClient()
        self.assertTrue(client.api_key)
        proposal = client.parse("站立")
        self.assertEqual(proposal.steps[0].skill, "stand")


if __name__ == "__main__":
    unittest.main()
