"""CPU tests for V5 pipeline bookkeeping helpers."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_foot_structure_optimization as pipeline  # noqa: E402


class FootPipelineTests(unittest.TestCase):
    def test_only_normal_and_marble_zero_four_cases(self):
        self.assertEqual(
            [(surface, slope) for surface, slope, _ in pipeline.CASES],
            [("normal", 0), ("normal", 4), ("marble", 0), ("marble", 4)],
        )
        self.assertNotIn("wood", {surface for surface, _, _ in pipeline.CASES})

    def test_training_metrics_use_final_iteration(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "train.log"
            log.write_text(
                "Mean reward: 1.0\n"
                "Mean episode length: 2.0\n"
                "Episode_Reward/dance_pose_track: 0.1\n"
                "Episode_Reward/dance_feet_schedule: 0.2\n"
                "Episode_Termination/fell_over: 1.0\n"
                "Episode_Termination/nan_state: 0.0000\n"
                "Mean reward: 70.5\n"
                "Mean episode length: 480.0\n"
                "Episode_Reward/dance_pose_track: 3.2\n"
                "Episode_Reward/dance_feet_schedule: 1.1\n"
                "Episode_Termination/fell_over: 0.2500\n"
                "Episode_Termination/nan_state: 0.0000\n",
                encoding="utf-8",
            )
            metrics = pipeline.training_metrics(log)
        self.assertEqual(metrics["mean_reward"], 70.5)
        self.assertEqual(metrics["dance_pose_track"], 3.2)
        self.assertEqual(metrics["fell_over"], 0.25)
        self.assertEqual(metrics["nan_state"], 0.0)

    def test_latest_checkpoint_uses_numeric_iteration(self):
        runner = pipeline.Pipeline.__new__(pipeline.Pipeline)
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            for name in ("model_9.pt", "model_100.pt", "model_20.pt", "notes.txt"):
                (run / name).write_bytes(b"checkpoint")
            self.assertEqual(runner.latest_checkpoint(run).name, "model_100.pt")


if __name__ == "__main__":
    unittest.main()
