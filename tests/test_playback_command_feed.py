#!/usr/bin/env python3
"""Unit tests for scripts/playback_command_feed.py (M1-A protocol, no Isaac).

Covers task book §8 items 3 and 4 at the protocol level: field validation
(NaN/Inf, unknown fields, encoding conflicts, mutual exclusion, bad types),
dedup/renewal never restarting a finite command, out-of-order/stale
rejection, session/epoch binding, lease expiry / missing file / corrupt
file / writer-exit watchdog all entering an explicit fault-stop instead of
holding the old velocity, finite completion by sim time, reset interruption,
fault recovery only via a new valid command, and the stop three-semantics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import playback_command_feed as pcf  # noqa: E402


CONTROL_DT = 0.02


def make_command(**overrides) -> dict:
    command = {
        "protocol_version": 1,
        "session_id": "sess",
        "session_epoch": 1,
        "command_id": "cmd-1",
        "sequence": 1,
        "skill_id": "walk",
        "contract_version": "sha256:" + "0" * 64,
        "kind": "velocity_setpoint",
        "encoding": "velocity",
        "params": {"velocity": {"vx": 0.15, "vy": 0.0, "yaw": 0.0}},
        "coordinate_frame": "base",
        "units": {"linear": "m/s", "angular": "rad/s"},
        "sim_duration_s": None,
        "lease_s": 5.0,
        "issued_at": "2026-10-27T00:00:00+00:00",
    }
    command.update(overrides)
    return command


class FeedHarness:
    """CommandFeed wired to a tmp command file."""

    def __init__(self, tmp: Path, watchdog_wall_s: float = 5.0):
        self.tmp = tmp
        self.command_path = tmp / "command.json"
        self.ack_path = tmp / "ack.json"
        self.feed = pcf.CommandFeed(
            self.command_path,
            session_id="sess",
            session_epoch=1,
            control_dt_s=CONTROL_DT,
            ack_path=self.ack_path,
            watchdog_wall_s=watchdog_wall_s,
        )
        self._seq = 0

    def write(self, **overrides) -> dict:
        self._seq += 1
        overrides.setdefault("sequence", self._seq)
        overrides.setdefault("command_id", f"cmd-{self._seq}")
        pcf.atomic_write_json(self.command_path, make_command(**overrides))
        return overrides

    def rewrite_raw(self, payload) -> None:
        pcf.atomic_write_json(self.command_path, payload)

    def age_file(self, seconds: float) -> None:
        """Push the command file mtime into the past (writer-stall模拟)."""
        old = time.time() - seconds
        os.utime(self.command_path, (old, old))

    def poll(self, steps: int = 1, start_step: int = 0) -> None:
        for i in range(steps):
            self.feed.poll(sim_step=start_step + i)

    def acks(self) -> list[dict]:
        payload = pcf.read_json(self.ack_path) or {}
        return payload.get("acks", [])

    def results(self) -> dict:
        payload = pcf.read_json(self.ack_path) or {}
        return payload.get("results", {})


class CommandValidationTests(unittest.TestCase):
    def parse(self, **overrides):
        return pcf.Command.from_dict(make_command(**overrides))

    def test_valid_command(self):
        command = self.parse()
        self.assertEqual(command.velocity(), (0.15, 0.0, 0.0))

    def test_unknown_field_rejected(self):
        with self.assertRaises(pcf.ProtocolError):
            self.parse(foo=1)

    def test_missing_field_rejected(self):
        raw = make_command()
        del raw["lease_s"]
        with self.assertRaises(pcf.ProtocolError):
            pcf.Command.from_dict(raw)

    def test_nan_inf_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(pcf.ProtocolError):
                self.parse(params={"velocity": {"vx": bad}})
        with self.assertRaises(pcf.ProtocolError):
            self.parse(lease_s=float("inf"))
        # JSON-level NaN literal is rejected at parse time
        with self.assertRaises(pcf.ProtocolError):
            pcf._loads_strict('{"vx": NaN}')

    def test_encoding_conflict_rejected(self):
        with self.assertRaises(pcf.ProtocolError):
            self.parse(encoding="velocity", params={"phase": {"period_s": 1.0}})
        # mutually exclusive: two typed blocks at once
        with self.assertRaises(pcf.ProtocolError):
            self.parse(params={"velocity": {"vx": 0.1}, "constant": {}})
        # encoding says phase but params carry velocity
        with self.assertRaises(pcf.ProtocolError):
            self.parse(encoding="phase", params={"velocity": {"vx": 0.1}})

    def test_invalid_types_rejected(self):
        with self.assertRaises(pcf.ProtocolError):
            self.parse(protocol_version=2)
        with self.assertRaises(pcf.ProtocolError):
            self.parse(sequence=True)  # bool is not an int here
        with self.assertRaises(pcf.ProtocolError):
            self.parse(session_epoch=0)
        with self.assertRaises(pcf.ProtocolError):
            self.parse(kind="teleport")
        with self.assertRaises(pcf.ProtocolError):
            self.parse(coordinate_frame="mars")
        with self.assertRaises(pcf.ProtocolError):
            self.parse(units={"linear": "cm/s", "angular": "rad/s"})
        with self.assertRaises(pcf.ProtocolError):
            self.parse(sim_duration_s=-1.0)
        with self.assertRaises(pcf.ProtocolError):
            self.parse(lease_s=None)  # streaming setpoint needs a lease
        with self.assertRaises(pcf.ProtocolError):
            self.parse(params={"velocity": {"vx": "fast"}})

    def test_control_kinds_take_no_params(self):
        command = self.parse(
            kind="stop_skill", params={}, sim_duration_s=None, lease_s=None
        )
        self.assertEqual(command.kind, "stop_skill")
        with self.assertRaises(pcf.ProtocolError):
            self.parse(kind="pause_sim", params={"velocity": {"vx": 0.1}})


class FeedApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = FeedHarness(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_acks_with_step_and_sim_time(self):
        self.h.write()
        self.h.poll(steps=3, start_step=10)
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))
        ack = self.h.acks()[-1]
        self.assertEqual(ack["status"], "applied")
        self.assertEqual(ack["applied_step"], 10)
        self.assertAlmostEqual(ack["sim_time_s"], 10 * CONTROL_DT)
        self.assertFalse(ack["renewal"])

    def test_duplicate_reread_is_noop(self):
        self.h.write()
        self.h.poll()
        self.h.poll(steps=5, start_step=1)  # same file re-read every step
        self.assertEqual(len(self.h.acks()), 1)  # applied exactly once

    def test_renewal_extends_lease_without_restart(self):
        self.h.write(sim_duration_s=1.0, lease_s=5.0)
        self.h.poll()  # applied at sim_time 0
        # renew at sim step 10 (t=0.2): same command_id, higher sequence
        self.h.write(command_id="cmd-1", sim_duration_s=1.0, lease_s=5.0)
        self.h.poll(steps=1, start_step=10)
        renewals = [a for a in self.h.acks() if a["renewal"]]
        self.assertEqual(len(renewals), 1)
        # completion still measured from the ORIGINAL applied time (t=0):
        # finish 1.0 s after apply == step 50, not step 60
        self.h.poll(steps=39, start_step=11)  # steps 11..49
        self.assertNotIn("cmd-1", self.h.results())
        self.h.poll(steps=1, start_step=50)
        result = self.h.results().get("cmd-1")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["applied_step"], 0)
        self.assertEqual(result["sim_steps_executed"], 50)

    def test_conflicting_renewal_rejected(self):
        self.h.write()
        self.h.poll()
        self.h.write(command_id="cmd-1", params={"velocity": {"vx": 0.3}})
        self.h.poll(start_step=1)
        ack = self.h.acks()[-1]
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("new command_id", ack["reason"])
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))

    def test_out_of_order_sequence_rejected(self):
        self.h.write()  # seq 1
        self.h.write()  # seq 2
        self.h.poll()
        stale = make_command(command_id="cmd-late", sequence=1)
        self.h.rewrite_raw(stale)
        self.h.poll(start_step=1)
        ack = self.h.acks()[-1]
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("out-of-order", ack["reason"])

    def test_wrong_session_rejected(self):
        self.h.write(session_id="other-session")
        self.h.poll()
        ack = self.h.acks()[-1]
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("session_id/epoch", ack["reason"])
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_wrong_epoch_rejected(self):
        self.h.write(session_epoch=2)
        self.h.poll()
        self.assertEqual(self.h.acks()[-1]["status"], "rejected")

    def test_unsupported_encoding_rejected(self):
        self.h.write(encoding="phase", params={"phase": {"period_s": 4.0}})
        self.h.poll()
        ack = self.h.acks()[-1]
        self.assertEqual(ack["status"], "rejected")
        self.assertIn("not supported", ack["reason"])


class FeedFaultTests(unittest.TestCase):
    """§8.4: missing/corrupt/expired/writer-exit never hold old velocity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = FeedHarness(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_lease_expiry_faults_and_zeroes(self):
        self.h.write(lease_s=0.2)
        self.h.poll()  # applied at t=0
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))
        self.h.poll(steps=11, start_step=1)  # t=0.22 > lease 0.2
        self.assertEqual(self.h.feed.fault, "lease_expired")
        self.assertEqual(self.h.feed.run_state, "fault_stopped")
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_missing_file_faults(self):
        self.h.write(lease_s=100.0)
        self.h.poll()
        self.h.command_path.unlink()  # file lost
        self.h.poll(steps=2, start_step=1)
        self.assertEqual(self.h.feed.fault, "command_file_missing")
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_corrupt_file_faults_after_lease(self):
        self.h.write(lease_s=0.1)
        self.h.poll()
        self.h.command_path.write_text("{not json", encoding="utf-8")
        self.h.poll(steps=6, start_step=1)  # fresh mtime, but lease runs out
        self.assertEqual(self.h.feed.fault, "lease_expired")
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_writer_exit_watchdog_faults(self):
        self.h.write(lease_s=100.0)  # long lease: only the watchdog can fire
        self.h.poll()
        self.h.age_file(10.0)  # writer stopped refreshing the file
        self.h.poll(start_step=1)
        self.assertEqual(self.h.feed.fault, "writer_watchdog_timeout")
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_fault_cleared_only_by_new_valid_command(self):
        self.h.write(lease_s=0.1)
        self.h.poll()
        self.h.poll(steps=6, start_step=1)
        self.assertIsNotNone(self.h.feed.fault)
        # re-reading the SAME command does not recover motion
        self.h.poll(start_step=10)
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))
        # a new valid request recovers
        self.h.write(command_id="cmd-new", sequence=50)
        self.h.poll(start_step=11)
        self.assertIsNone(self.h.feed.fault)
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))


class FeedCompletionAndResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = FeedHarness(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_finite_command_completes_by_sim_time(self):
        self.h.write(sim_duration_s=0.5, lease_s=10.0)
        self.h.poll()  # applied at step 0
        self.h.poll(steps=24, start_step=1)
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))
        self.h.poll(steps=1, start_step=25)  # t=0.5 reached
        result = self.h.results()["cmd-1"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["applied_step"], 0)
        self.assertEqual(result["finished_step"], 25)
        self.assertEqual(result["sim_steps_executed"], 25)
        # setpoint falls back to zero after completion
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))

    def test_reset_interrupts_active_command(self):
        self.h.write(sim_duration_s=5.0, lease_s=10.0)
        self.h.poll()
        self.h.feed.on_episode_reset(episode_id=1, reason="time_out", sim_step=20, sim_time_s=0.4)
        result = self.h.results()["cmd-1"]
        self.assertEqual(result["status"], "interrupted_by_reset")
        self.assertEqual(result["episode_id"], 1)
        self.assertEqual(self.h.feed.episode_id, 1)
        self.assertEqual(self.h.feed.reset_reason, "time_out")
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))
        # session is not faulted: a new command applies cleanly
        self.h.write(command_id="cmd-2", sequence=2)
        self.h.poll(start_step=21)
        self.assertIsNone(self.h.feed.fault)
        self.assertEqual(self.h.feed.current_velocity(), (0.15, 0.0, 0.0))


class FeedStopSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = FeedHarness(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_stop_skill_zeroes_and_reports(self):
        self.h.write(lease_s=100.0)
        self.h.poll()
        self.h.write(kind="stop_skill", params={}, lease_s=None)
        self.h.poll(start_step=5)
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))
        self.assertIsNone(self.h.feed.fault)
        self.assertEqual(self.h.feed.run_state, "running")  # sim keeps running
        self.assertEqual(self.h.results()["cmd-1"]["status"], "stopped")

    def test_pause_and_resume(self):
        self.h.write(lease_s=100.0)
        self.h.poll()
        self.h.write(kind="pause_sim", params={}, lease_s=None)
        self.h.poll(start_step=1)
        self.assertTrue(self.h.feed.paused)
        self.assertEqual(self.h.feed.run_state, "paused")
        self.h.write(kind="resume_sim", params={}, lease_s=None)
        self.h.poll(start_step=2)
        self.assertFalse(self.h.feed.paused)

    def test_cancel_session_latches(self):
        self.h.write(lease_s=100.0)
        self.h.poll()
        self.h.write(kind="cancel_session", params={}, lease_s=None)
        self.h.poll(start_step=1)
        self.assertTrue(self.h.feed.cancel_requested)
        self.assertEqual(self.h.feed.run_state, "cancelled")
        # nothing applies after cancel, even a fresh valid command
        self.h.write(command_id="cmd-after", sequence=10)
        self.h.poll(start_step=2)
        self.assertEqual(self.h.feed.current_velocity(), (0.0, 0.0, 0.0))


class StatusSnapshotTests(unittest.TestCase):
    def test_atomic_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            pcf.write_status(path, {"sim_step": 1, "wall_updated_at": time.time()})
            snapshot = pcf.read_json(path)
            self.assertEqual(snapshot["sim_step"], 1)
            # atomic replace leaves no tmp file behind
            self.assertEqual(list(Path(tmp).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
