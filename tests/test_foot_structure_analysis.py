"""CPU tests for offline V5 foot-structure analysis and selection."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import analyze_foot_structure as foot  # noqa: E402


def amplitude_report(median=1.0, right_knee=1.0, right_ankle=1.0):
    return {
        "median_amplitude_ratio": median,
        "valid_step_count": 20,
        "joints": [
            {
                "name": "right_knee",
                "actual_amplitude_rad": right_knee,
                "target_amplitude_rad": 1.0,
                "amplitude_ratio": right_knee,
            },
            {
                "name": "right_ankle",
                "actual_amplitude_rad": right_ankle,
                "target_amplitude_rad": 1.0,
                "amplitude_ratio": right_ankle,
            },
        ],
    }


def synthetic_trace(left_x=(0.0, 1.0, 100.0, 101.0), dt_s=0.5):
    right_loaded = [True, True, False, False]
    done = [False, False, True, False]
    rows = []
    for index, x in enumerate(left_x):
        rows.append(
            {
                "step": index,
                "dt_s": dt_s,
                "done": [done[index]],
                "root_position": [[0.1 * index, 0.0, 0.12]],
                "joint_position": [[[0.0] * 14][0]],
                "foot_position": [
                    [[x, 0.0, 0.01], [0.0, 0.0, 0.01]]
                ],
                "foot_linear_velocity": [[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]],
                "foot_force": [[[0.0, 0.0, 10.0], [0.0, 0.0, 10.0]]],
                "foot_loaded": [[True, right_loaded[index]]],
            }
        )
    return rows


def acceptance_for(rows, displacement=1.25):
    return {
        "steps": len(rows),
        "num_envs": 1,
        "done_count": sum(int(row["done"][0]) for row in rows),
        "trace_feet_names": ["left", "right"],
        "mean_root_displacement_m": displacement,
        "forward_displacement_m_per_env": [displacement],
        "lateral_displacement_m_per_env": [0.0],
        "terrain": {"slope_deg": 0.0},
    }


def minimal_analysis(
    displacement,
    slide,
    *,
    median=1.0,
    right_knee=1.0,
    right_ankle=1.0,
    unexpected_resets=0,
    data_complete=True,
):
    return {
        "root_planar_displacement_m": displacement,
        "total_slide_distance_m": slide,
        "amplitude": {
            "median_amplitude_ratio": median,
            "joints": {
                "right_knee": {"amplitude_ratio": right_knee},
                "right_ankle": {"amplitude_ratio": right_ankle},
            },
        },
        "data_complete": data_complete,
        "data_issues": [] if data_complete else ["synthetic_incomplete_data"],
        "unexpected_reset_count": unexpected_resets,
    }


def selection_summary():
    cases = [foot.case_id(surface, slope) for surface, slope in foot.EXPECTED_CASES]
    summary = {
        "variants": {
            variant: {
                "cases": {
                    case: {"status": "missing", "error": "not supplied"}
                    for case in cases
                }
            }
            for variant in foot.EXPECTED_VARIANTS
        }
    }
    original_displacement = (10.0, 20.0, 30.0, 40.0)
    original_slide = (2.0, 4.0, 6.0, 8.0)
    for index, case in enumerate(cases):
        summary["variants"]["original"]["cases"][case] = {
            "status": "ok",
            "analysis": minimal_analysis(
                original_displacement[index], original_slide[index]
            ),
        }
    return summary


def install_candidate(
    summary,
    variant,
    displacement_ratios=(0.9, 0.9, 0.9, 0.9),
    slide_ratios=(0.5, 0.5, 0.5, 0.5),
    **amplitude_kwargs,
):
    cases = [foot.case_id(surface, slope) for surface, slope in foot.EXPECTED_CASES]
    for index, case in enumerate(cases):
        original = summary["variants"]["original"]["cases"][case]["analysis"]
        summary["variants"][variant]["cases"][case] = {
            "status": "ok",
            "analysis": minimal_analysis(
                original["root_planar_displacement_m"] * displacement_ratios[index],
                original["total_slide_distance_m"] * slide_ratios[index],
                **amplitude_kwargs,
            ),
        }


class FootSceneAnalysisTests(unittest.TestCase):
    def analyze(self, rows=None, **kwargs):
        rows = synthetic_trace() if rows is None else rows
        return foot.analyze_scene_data(
            acceptance_for(rows),
            rows,
            amplitude_report(),
            variant="W090",
            surface="normal",
            slope_deg=0.0,
            **kwargs,
        )

    def test_loaded_sliding_support_and_reset_exclusion(self):
        result = self.analyze()
        left = result["feet"]["left"]
        right = result["feet"]["right"]
        self.assertTrue(result["slide_is_foot_site_proxy"])
        self.assertEqual(result["dt_s"], 0.5)
        self.assertEqual(result["dt_source"], "trace:dt_s")
        self.assertEqual(left["loaded_time_s"], 2.0)
        self.assertEqual(right["loaded_time_s"], 1.0)
        self.assertEqual(left["cumulative_slide_distance_m"], 2.0)
        self.assertEqual(left["slide_transition_count"], 2)
        self.assertEqual(left["mean_slide_speed_mps"], 2.0)
        self.assertEqual(left["p95_slide_speed_mps"], 2.0)
        self.assertEqual(right["cumulative_slide_distance_m"], 0.0)
        self.assertEqual(right["mean_slide_speed_mps"], 0.0)
        self.assertEqual(result["total_slide_distance_m"], 2.0)
        self.assertEqual(result["double_support_time_s"], 1.0)
        self.assertEqual(result["single_support_time_s"], 1.0)
        self.assertEqual(result["root_planar_displacement_m"], 1.25)
        self.assertEqual(result["unexpected_reset_count"], 1)

    def test_argument_dt_overrides_trace_dt(self):
        result = self.analyze(dt_s=0.25)
        self.assertEqual(result["dt_source"], "argument")
        self.assertEqual(result["feet"]["left"]["loaded_time_s"], 1.0)
        self.assertEqual(result["feet"]["left"]["mean_slide_speed_mps"], 4.0)

    def test_expected_final_timeout_is_not_an_extra_reset(self):
        rows = synthetic_trace(left_x=(0.0, 0.0))[-2:]
        rows[0]["step"] = 998
        rows[1]["step"] = 999
        rows[0]["done"] = [False]
        rows[1]["done"] = [True]
        rows[0]["time_out"] = [False]
        rows[1]["time_out"] = [True]
        result = self.analyze(rows)
        self.assertEqual(result["normal_time_out_count"], 1)
        self.assertEqual(result["unexpected_reset_count"], 0)

    def test_missing_and_nonfinite_foot_data_are_rejected(self):
        rows = synthetic_trace()
        del rows[0]["foot_force"]
        with self.assertRaisesRegex(ValueError, "foot_force"):
            self.analyze(rows)

        rows = synthetic_trace()
        rows[1]["foot_position"][0][0][0] = float("nan")
        with self.assertRaisesRegex(ValueError, "NaN or infinity"):
            self.analyze(rows)


class FootSelectionTests(unittest.TestCase):
    def test_score_formula_and_winner(self):
        summary = selection_summary()
        install_candidate(summary, "W090")
        selection = foot.select_variant(summary)
        result = selection["variants"]["W090"]
        original_slides = np.asarray([2.0, 4.0, 6.0, 8.0])
        candidate_slides = original_slides * 0.5
        expected_slide_mean = np.mean(
            (candidate_slides + 0.001) / (original_slides + 0.001)
        )
        expected_j = 0.75 * 0.9 + 0.25 * expected_slide_mean
        self.assertTrue(result["technical_gate_passed"])
        self.assertTrue(result["eligible"])
        self.assertAlmostEqual(result["score_j"], expected_j)
        self.assertEqual(selection["winner"], "W090")
        self.assertFalse(selection["variants"]["original"]["eligible"])
        self.assertNotEqual(selection["winner"], "original")

    def test_each_amplitude_gate_blocks_candidate(self):
        for kwargs, reason in (
            ({"median": 0.94}, "amplitude_gate:median_amplitude_ratio"),
            ({"right_knee": 0.94}, "amplitude_gate:right_knee"),
            ({"right_ankle": 0.94}, "amplitude_gate:right_ankle"),
        ):
            with self.subTest(reason=reason):
                summary = selection_summary()
                install_candidate(summary, "W115", **kwargs)
                result = foot.select_variant(summary)["variants"]["W115"]
                self.assertFalse(result["technical_gate_passed"])
                self.assertFalse(result["eligible"])
                self.assertIsNone(result["score_j"])
                self.assertTrue(any(reason in item for item in result["gate_failure_reasons"]))

    def test_reset_nan_and_incomplete_data_are_negative_controls(self):
        summary = selection_summary()
        install_candidate(summary, "W090", unexpected_resets=1)
        install_candidate(summary, "W115", data_complete=False)
        install_candidate(summary, "W130")
        nan_case = foot.case_id("normal", 0.0)
        summary["variants"]["W130"]["cases"][nan_case]["analysis"][
            "root_planar_displacement_m"
        ] = float("nan")
        selection = foot.select_variant(summary)
        for variant in ("W090", "W115", "W130"):
            with self.subTest(variant=variant):
                self.assertFalse(selection["variants"][variant]["technical_gate_passed"])
                self.assertFalse(selection["variants"][variant]["eligible"])
        self.assertIsNone(selection["winner"])

    def test_displacement_constraints_are_negative_controls(self):
        summary = selection_summary()
        install_candidate(summary, "W090", displacement_ratios=(1.0, 1.0, 1.0, 1.0))
        install_candidate(summary, "W115", displacement_ratios=(1.11, 0.9, 0.9, 0.9))
        selection = foot.select_variant(summary)
        self.assertFalse(selection["variants"]["W090"]["eligible"])
        self.assertTrue(
            any("displacement_not_better" in reason for reason in selection["variants"]["W090"]["eligibility_reasons"])
        )
        self.assertFalse(selection["variants"]["W115"]["eligible"])
        self.assertTrue(
            any("regression_over_10_percent" in reason for reason in selection["variants"]["W115"]["eligibility_reasons"])
        )
        self.assertIsNone(selection["winner"])

    def test_missing_variant_cannot_win(self):
        selection = foot.select_variant(selection_summary())
        self.assertIsNone(selection["winner"])
        for variant in foot.EXPECTED_VARIANTS[1:]:
            self.assertFalse(selection["variants"][variant]["eligible"])
            self.assertFalse(selection["variants"][variant]["technical_gate_passed"])


class FootReportTests(unittest.TestCase):
    def write_scene(self, root: Path, rows):
        (root / "acceptance.json").write_text(
            json.dumps(acceptance_for(rows)), encoding="utf-8"
        )
        (root / "trace.json").write_text(json.dumps(rows), encoding="utf-8")
        (root / "amplitude.json").write_text(
            json.dumps(amplitude_report()), encoding="utf-8"
        )

    def test_report_plots_and_lists_every_missing_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = synthetic_trace(left_x=(0.0, 0.0, 0.0, 0.0))
            for row in rows:
                row["done"] = [False]
            self.write_scene(root, rows)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "variants": {
                            "original": {
                                "normal-slope-0": {
                                    "acceptance": "acceptance.json",
                                    "trace": "trace.json",
                                    "amplitude": "amplitude.json",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "output"
            summary, selection = foot.generate_report(manifest, output)
            self.assertEqual(len(summary["missing_cases"]), 27)
            self.assertEqual(len(summary["failed_cases"]), 0)
            self.assertFalse(summary["complete"])
            self.assertEqual(
                summary["variants"]["W130"]["cases"]["marble-slope-4"]["status"],
                "missing",
            )
            for name in ("displacement", "sliding", "amplitude"):
                path = Path(summary["plots"][name])
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "selection.json").is_file())
            self.assertIsNone(selection["winner"])

    def test_failed_scene_is_explicit_and_never_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = synthetic_trace()
            del rows[2]["foot_loaded"]
            self.write_scene(root, rows)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "variants": {
                            "W090": {
                                "normal-slope-0": {
                                    "acceptance": "acceptance.json",
                                    "trace": "trace.json",
                                    "amplitude": "amplitude.json",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            summary, selection = foot.generate_report(
                manifest, root / "output", make_plots=False
            )
            entry = summary["variants"]["W090"]["cases"]["normal-slope-0"]
            self.assertEqual(entry["status"], "failed")
            self.assertIn("foot_loaded", entry["error"])
            self.assertEqual(len(summary["failed_cases"]), 1)
            case_result = selection["variants"]["W090"]["cases"]["normal-slope-0"]
            self.assertIsNone(case_result["displacement_ratio"])
            self.assertIsNone(case_result["slide_ratio_with_epsilon"])
            self.assertFalse(selection["variants"]["W090"]["eligible"])


if __name__ == "__main__":
    unittest.main()
