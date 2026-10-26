"""CPU tests for the V5 hash-chained event ledger."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_foot_structure_report as report  # noqa: E402
import foot_structure_ledger as ledger  # noqa: E402


def write_lines(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class LedgerChainTests(unittest.TestCase):
    def test_append_chains_seq_and_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            first = ledger.append_event(path, {"variant": "original", "stage": "convert", "status": "running"})
            second = ledger.append_event(path, {"variant": "original", "stage": "convert", "status": "success"})
            third = ledger.append_event(path, {"variant": "W090", "stage": "convert", "status": "failed"})
            self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])
            self.assertIsNone(first["prev_hash"])
            self.assertEqual(second["prev_hash"], first["event_hash"])
            self.assertEqual(third["prev_hash"], second["event_hash"])
            for event in (first, second, third):
                self.assertEqual(event["event_hash"], ledger.event_digest(event))
            self.assertEqual(ledger.verify_chain(path), [])

    def test_genesis_event_has_null_prev_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            event = ledger.append_event(path, {"variant": "original", "stage": "convert", "status": "success"})
            self.assertEqual(event["seq"], 1)
            self.assertIsNone(event["prev_hash"])

    def test_tampered_field_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            for index in range(3):
                ledger.append_event(path, {"variant": "original", "stage": f"s{index}", "status": "success"})
            events = list(ledger.iter_events(path))
            events[1]["status"] = "failed"
            write_lines(path, events)
            errors = ledger.verify_chain(path)
            self.assertTrue(any("event_hash" in error for error in errors))

    def test_deleted_line_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            for index in range(4):
                ledger.append_event(path, {"variant": "original", "stage": f"s{index}", "status": "success"})
            events = list(ledger.iter_events(path))
            del events[1]
            write_lines(path, events)
            errors = ledger.verify_chain(path)
            self.assertTrue(any("seq" in error for error in errors))

    def test_modified_event_hash_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            for index in range(3):
                ledger.append_event(path, {"variant": "original", "stage": f"s{index}", "status": "success"})
            events = list(ledger.iter_events(path))
            events[1]["event_hash"] = "0" * 64
            write_lines(path, events)
            errors = ledger.verify_chain(path)
            self.assertTrue(any("event_hash" in error for error in errors))

    def test_legacy_ledger_reports_migrate_hint(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            write_lines(path, [{"variant": "original", "stage": "convert", "status": "success"}])
            errors = ledger.verify_chain(path)
            self.assertTrue(any("migrate" in error for error in errors))

    def test_append_is_durable_across_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            ledger.append_event(path, {"variant": "original", "stage": "convert", "status": "success"})
            with path.open("r", encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage"], "convert")
            self.assertEqual(ledger.verify_chain(path), [])


class LedgerMigrationTests(unittest.TestCase):
    def test_migrate_preserves_fields_and_verifies(self):
        legacy = [
            {"variant": "original", "stage": "convert", "status": "running", "elapsed_s": 1.5},
            {"variant": "original", "stage": "convert", "status": "success", "elapsed_s": 2.5},
            {"variant": "W090", "stage": "convert", "status": "success", "metrics": {"a": 1}},
            {"variant": "W090", "stage": "train-seed42", "status": "failed", "reason": "x"},
            {"variant": "original", "stage": "optimization", "status": "failed", "verdict": {}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            src = Path(temporary) / "events.jsonl"
            dst = Path(temporary) / "events.jsonl.chained"
            write_lines(src, legacy)
            self.assertNotEqual(ledger.verify_chain(src), [])
            self.assertEqual(ledger.migrate(src, dst), 5)
            self.assertEqual(ledger.verify_chain(dst), [])
            chained = list(ledger.iter_events(dst))
            self.assertTrue(chained[0]["migrated_from_legacy"])
            for original, migrated in zip(legacy, chained):
                for key, value in original.items():
                    self.assertEqual(migrated[key], value)
            self.assertEqual([event["seq"] for event in chained], [1, 2, 3, 4, 5])


class ReportReplayTests(unittest.TestCase):
    def test_replay_takes_last_terminal_event(self):
        events = [
            {"variant": "W090", "stage": "convert", "status": "running"},
            {"variant": "W090", "stage": "convert", "status": "failed", "error": "first"},
            {"variant": "W090", "stage": "convert", "status": "running"},
            {"variant": "W090", "stage": "convert", "status": "success", "usd": "x"},
            {"variant": "W090", "stage": "train-seed42", "status": "running"},
            {"variant": "W115", "stage": "convert", "status": "skipped", "reason": "y"},
        ]
        replayed = report.replay_ledger(events)
        stages = replayed["variants"]["W090"]["stages"]
        self.assertEqual(stages["convert"]["status"], "success")
        self.assertNotIn("train-seed42", stages)
        self.assertEqual(replayed["variants"]["W115"]["stages"]["convert"]["status"], "skipped")

    def test_report_refuses_broken_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            events_path = work / "events.jsonl"
            for index in range(3):
                ledger.append_event(events_path, {"variant": "original", "stage": f"s{index}", "status": "success"})
            events = list(ledger.iter_events(events_path))
            events[1]["status"] = "failed"
            write_lines(events_path, events)
            with mock.patch.object(report, "WORK", work):
                with self.assertRaises(SystemExit):
                    report.build_report(work / "report.md")
            self.assertFalse((work / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
