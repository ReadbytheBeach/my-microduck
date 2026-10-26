#!/usr/bin/env python3
"""Offline V5 foot-structure analysis, comparison plots, and variant selection."""

from __future__ import annotations

import argparse
import json
import math
import numbers
from pathlib import Path
from typing import Any

import numpy as np


SCHEMA_VERSION = 1
EXPECTED_VARIANTS = ("original", "W090", "W115", "W130", "L110", "L110W115", "L110W130")
EXPECTED_CASES = tuple(
    (surface, slope)
    for surface in ("normal", "marble")
    for slope in (0.0, 4.0)
)
EXPECTED_TIMEOUT_STEP = 999
AMPLITUDE_GATE = 0.95
SLIDE_EPSILON_M = 0.001
DISPLACEMENT_WEIGHT = 0.75
SLIDE_WEIGHT = 0.25
FLAT_DISPLACEMENT_TOLERANCE = 1.10
REQUIRED_JOINTS = ("right_knee", "right_ankle")


def case_id(surface: str, slope_deg: float) -> str:
    return f"{surface}-slope-{slope_deg:g}"


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{path} contains non-finite JSON value {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _ensure_finite_json_values(value: Any, label: str) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif (
            isinstance(current, numbers.Real)
            and not isinstance(current, (bool, np.bool_))
            and not math.isfinite(float(current))
        ):
            raise ValueError(f"{label} contains NaN or infinity")


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Real):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _trace_parts(trace: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(trace, list):
        rows = trace
        metadata: dict[str, Any] = {}
    elif isinstance(trace, dict):
        if "rows" in trace:
            rows = trace["rows"]
        elif "trace" in trace:
            rows = trace["trace"]
        else:
            raise ValueError("Trace object must contain rows or trace")
        metadata = trace
    else:
        raise ValueError("Trace must be a JSON array or an object containing rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Trace must contain at least one row")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Every trace row must be an object")
    return rows, metadata


def _resolve_dt(
    rows: list[dict[str, Any]], metadata: dict[str, Any], dt_s: float | None
) -> tuple[float, str]:
    if dt_s is not None:
        result = _finite_float(dt_s, "dt_s")
        if result <= 0.0:
            raise ValueError("dt_s must be positive")
        return result, "argument"

    found: list[tuple[float, str]] = []
    for container, prefix in ((metadata, "trace_metadata"), *((row, "trace") for row in rows)):
        for key in ("dt_s", "control_dt_s", "dt"):
            if key in container:
                value = _finite_float(container[key], f"{prefix}.{key}")
                if value <= 0.0:
                    raise ValueError(f"{prefix}.{key} must be positive")
                found.append((value, f"{prefix}:{key}"))
    if not found:
        raise ValueError("Trace has no dt_s/control_dt_s/dt; pass --dt-s")
    first = found[0][0]
    if any(not math.isclose(first, value, rel_tol=1.0e-9, abs_tol=1.0e-12) for value, _ in found):
        raise ValueError("Trace contains inconsistent dt values")
    return first, found[0][1]


def _row_array(
    rows: list[dict[str, Any]], key: str, shape_tail: tuple[int, ...] | None = None
) -> np.ndarray:
    if any(key not in row for row in rows):
        raise ValueError(f"Trace is missing required field {key}")
    try:
        result = np.asarray([row[key] for row in rows], dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Trace field {key} is not a regular numeric array") from error
    if shape_tail is not None and result.shape[1:] != shape_tail:
        raise ValueError(
            f"Trace field {key} has shape {result.shape[1:]}, expected {shape_tail}"
        )
    if not np.isfinite(result).all():
        raise ValueError(f"Trace field {key} contains NaN or infinity")
    return result


def _boolean_array(rows: list[dict[str, Any]], key: str) -> np.ndarray:
    values = _row_array(rows, key)
    if not np.isin(values, (0.0, 1.0)).all():
        raise ValueError(f"Trace field {key} must contain only booleans or 0/1")
    return values.astype(bool)


def _optional_time_out(rows: list[dict[str, Any]], shape: tuple[int, int]) -> tuple[np.ndarray, bool]:
    key = next((name for name in ("time_out", "time_outs") if name in rows[0]), None)
    if key is None:
        return np.zeros(shape, dtype=bool), False
    values = _boolean_array(rows, key)
    if values.shape != shape:
        raise ValueError(f"Trace field {key} has shape {values.shape}, expected {shape}")
    return values, True


def _amplitude_metrics(amplitude: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(amplitude, dict):
        raise ValueError("Amplitude report must be an object")
    median = _finite_float(amplitude.get("median_amplitude_ratio"), "median_amplitude_ratio")
    joints_raw = amplitude.get("joints")
    if not isinstance(joints_raw, list):
        raise ValueError("Amplitude report is missing joints")
    joints_by_name: dict[str, dict[str, Any]] = {}
    for joint in joints_raw:
        if isinstance(joint, dict) and isinstance(joint.get("name"), str):
            joints_by_name[joint["name"]] = joint
    joints: dict[str, Any] = {}
    for name in REQUIRED_JOINTS:
        if name not in joints_by_name:
            raise ValueError(f"Amplitude report is missing {name}")
        joint = joints_by_name[name]
        joints[name] = {
            "actual_amplitude_rad": _finite_float(
                joint.get("actual_amplitude_rad"), f"{name}.actual_amplitude_rad"
            ),
            "amplitude_ratio": _finite_float(
                joint.get("amplitude_ratio"), f"{name}.amplitude_ratio"
            ),
        }
        if "target_amplitude_rad" in joint:
            joints[name]["target_amplitude_rad"] = _finite_float(
                joint["target_amplitude_rad"], f"{name}.target_amplitude_rad"
            )
    return {
        "median_amplitude_ratio": median,
        "joints": joints,
        "valid_step_count": amplitude.get("valid_step_count"),
    }


def analyze_scene_data(
    acceptance: dict[str, Any],
    trace: Any,
    amplitude: dict[str, Any],
    *,
    variant: str,
    surface: str,
    slope_deg: float,
    dt_s: float | None = None,
) -> dict[str, Any]:
    """Analyze one scene and raise ValueError for incomplete or non-finite input."""
    _ensure_finite_json_values(acceptance, "Acceptance")
    _ensure_finite_json_values(trace, "Trace")
    _ensure_finite_json_values(amplitude, "Amplitude report")
    rows, trace_metadata = _trace_parts(trace)
    resolved_dt, dt_source = _resolve_dt(rows, trace_metadata, dt_s)
    slope = _finite_float(slope_deg, "slope_deg")

    steps_raw = _row_array(rows, "step")
    if steps_raw.ndim != 1 or not np.equal(steps_raw, np.floor(steps_raw)).all():
        raise ValueError("Trace steps must be a one-dimensional integer sequence")
    steps = steps_raw.astype(np.int64)
    if len(steps) > 1 and not np.all(np.diff(steps) == 1):
        raise ValueError("Trace steps must be contiguous and strictly increasing")

    done = _boolean_array(rows, "done")
    if done.ndim != 2:
        raise ValueError(f"Trace field done must have shape [steps][num_envs], got {done.shape}")
    step_count, num_envs = done.shape
    feet_names = acceptance.get("trace_feet_names")
    if not isinstance(feet_names, list) or len(feet_names) != 2 or not all(
        isinstance(name, str) and name for name in feet_names
    ):
        raise ValueError("Acceptance trace_feet_names must contain two foot names")
    if len(set(feet_names)) != 2:
        raise ValueError("Acceptance trace_feet_names must be unique")

    foot_position = _row_array(rows, "foot_position", (num_envs, 2, 3))
    foot_linear_velocity = _row_array(rows, "foot_linear_velocity", (num_envs, 2, 3))
    foot_force = _row_array(rows, "foot_force", (num_envs, 2, 3))
    foot_loaded = _boolean_array(rows, "foot_loaded")
    if foot_loaded.shape != (step_count, num_envs, 2):
        raise ValueError(
            "Trace field foot_loaded must have shape [steps][num_envs][2], "
            f"got {foot_loaded.shape}"
        )
    root_position = _row_array(rows, "root_position", (num_envs, 3))
    joint_position = _row_array(rows, "joint_position")
    if joint_position.ndim != 3 or joint_position.shape[:2] != (step_count, num_envs):
        raise ValueError("Trace field joint_position must have shape [steps][num_envs][joints]")
    del foot_linear_velocity, foot_force, root_position, joint_position

    time_out, time_out_available = _optional_time_out(rows, done.shape)
    amplitude_metrics = _amplitude_metrics(amplitude)
    root_displacement = _finite_float(
        acceptance.get("mean_root_displacement_m"), "mean_root_displacement_m"
    )
    if root_displacement < 0.0:
        raise ValueError("mean_root_displacement_m cannot be negative")

    data_issues: list[str] = []
    if "steps" in acceptance and acceptance["steps"] != step_count:
        data_issues.append(
            f"acceptance_steps_mismatch:{acceptance['steps']}!={step_count}"
        )
    if "num_envs" in acceptance and acceptance["num_envs"] != num_envs:
        data_issues.append(
            f"acceptance_num_envs_mismatch:{acceptance['num_envs']}!={num_envs}"
        )
    done_count = int(done.sum())
    acceptance_done_count = acceptance.get("done_count")
    if acceptance_done_count is None:
        data_issues.append("acceptance_done_count_missing")
    elif acceptance_done_count != done_count:
        data_issues.append(
            f"acceptance_done_count_mismatch:{acceptance_done_count}!={done_count}"
        )
    if np.any(time_out & ~done):
        data_issues.append("time_out_true_without_done")
    if "time_out_count" in acceptance and acceptance["time_out_count"] != int(time_out.sum()):
        data_issues.append("acceptance_time_out_count_mismatch")

    reset_events: list[dict[str, Any]] = []
    for row_index, env_index in np.argwhere(done):
        timeout_value = bool(time_out[row_index, env_index]) if time_out_available else None
        expected = int(steps[row_index]) == EXPECTED_TIMEOUT_STEP and (
            timeout_value is True or not time_out_available
        )
        reset_events.append(
            {
                "step": int(steps[row_index]),
                "env_index": int(env_index),
                "time_out": timeout_value,
                "expected_time_out": bool(expected),
            }
        )
    unexpected_reset_count = sum(not event["expected_time_out"] for event in reset_events)

    loaded_count = foot_loaded.sum(axis=2)
    support = {
        "single_support_time_s": float(np.count_nonzero(loaded_count == 1) * resolved_dt),
        "double_support_time_s": float(np.count_nonzero(loaded_count == 2) * resolved_dt),
        "no_support_time_s": float(np.count_nonzero(loaded_count == 0) * resolved_dt),
    }
    support["single_support_time_s_mean_per_env"] = support["single_support_time_s"] / num_envs
    support["double_support_time_s_mean_per_env"] = support["double_support_time_s"] / num_envs
    support["no_support_time_s_mean_per_env"] = support["no_support_time_s"] / num_envs

    if step_count > 1:
        deltas = np.linalg.norm(
            foot_position[1:, :, :, :2] - foot_position[:-1, :, :, :2], axis=-1
        )
        current_reset = done[1:]
        valid = (
            foot_loaded[1:]
            & foot_loaded[:-1]
            & ~current_reset[:, :, np.newaxis]
        )
    else:
        deltas = np.empty((0, num_envs, 2), dtype=np.float64)
        valid = np.empty((0, num_envs, 2), dtype=bool)

    feet: dict[str, Any] = {}
    for foot_index, name in enumerate(feet_names):
        distances = deltas[:, :, foot_index][valid[:, :, foot_index]]
        speeds = distances / resolved_dt
        feet[name] = {
            "loaded_step_count": int(foot_loaded[:, :, foot_index].sum()),
            "loaded_time_s": float(foot_loaded[:, :, foot_index].sum() * resolved_dt),
            "loaded_time_s_mean_per_env": float(
                foot_loaded[:, :, foot_index].sum() * resolved_dt / num_envs
            ),
            "cumulative_slide_distance_m": float(distances.sum()),
            "mean_slide_speed_mps": float(speeds.mean()) if len(speeds) else None,
            "p95_slide_speed_mps": float(np.percentile(speeds, 95)) if len(speeds) else None,
            "slide_transition_count": int(len(distances)),
        }

    total_slide_per_env = deltas.sum(axis=2)
    total_slide_per_env = np.where(valid.any(axis=2), total_slide_per_env, 0.0)
    per_env_slide = []
    for env_index in range(num_envs):
        per_env_slide.append(float(deltas[:, env_index, :][valid[:, env_index, :]].sum()))
    total_slide = float(sum(foot["cumulative_slide_distance_m"] for foot in feet.values()))
    del total_slide_per_env

    root_displacement_per_env: list[float] | None = None
    forward = acceptance.get("forward_displacement_m_per_env")
    lateral = acceptance.get("lateral_displacement_m_per_env")
    if (
        isinstance(forward, list)
        and isinstance(lateral, list)
        and len(forward) == num_envs
        and len(lateral) == num_envs
    ):
        root_displacement_per_env = [
            math.hypot(
                _finite_float(x, "forward_displacement_m_per_env"),
                _finite_float(y, "lateral_displacement_m_per_env"),
            )
            for x, y in zip(forward, lateral, strict=True)
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "analysis": "foot_structure_scene",
        "variant": variant,
        "surface": surface,
        "slope_deg": slope,
        "case": case_id(surface, slope),
        "dt_s": resolved_dt,
        "dt_source": dt_source,
        "step_count": step_count,
        "first_step": int(steps[0]),
        "last_step": int(steps[-1]),
        "num_envs": num_envs,
        "trace_feet_names": list(feet_names),
        "slide_is_foot_site_proxy": True,
        "slide_definition": (
            "XY foot-site displacement for adjacent frames when the same foot is loaded "
            "in both frames and the current frame has no done/reset"
        ),
        "feet": feet,
        "total_slide_distance_m": total_slide,
        "total_slide_distance_m_per_env": per_env_slide,
        **support,
        "root_planar_displacement_m": root_displacement,
        "root_planar_displacement_m_per_env": root_displacement_per_env,
        "root_displacement_source": "acceptance.mean_root_displacement_m",
        "amplitude": amplitude_metrics,
        "done_count": done_count,
        "acceptance_done_count": acceptance_done_count,
        "reset_count": len(reset_events),
        "normal_time_out_count": len(reset_events) - unexpected_reset_count,
        "unexpected_reset_count": unexpected_reset_count,
        "reset_events": reset_events,
        "time_out_available": time_out_available,
        "expected_timeout_step": EXPECTED_TIMEOUT_STEP,
        "data_complete": not data_issues,
        "data_issues": data_issues,
        "finite": True,
        "metadata": {
            "checkpoint": acceptance.get("checkpoint"),
            "seed": acceptance.get("seed"),
            "timestamp_utc": acceptance.get("timestamp_utc"),
            "terrain": acceptance.get("terrain"),
        },
    }


def analyze_scene(
    acceptance_path: Path,
    trace_path: Path,
    amplitude_path: Path,
    *,
    variant: str,
    surface: str,
    slope_deg: float,
    dt_s: float | None = None,
) -> dict[str, Any]:
    acceptance_path = acceptance_path.expanduser().resolve()
    trace_path = trace_path.expanduser().resolve()
    amplitude_path = amplitude_path.expanduser().resolve()
    result = analyze_scene_data(
        _load_json(acceptance_path),
        _load_json(trace_path),
        _load_json(amplitude_path),
        variant=variant,
        surface=surface,
        slope_deg=slope_deg,
        dt_s=dt_s,
    )
    result["source"] = {
        "acceptance": str(acceptance_path),
        "trace": str(trace_path),
        "amplitude": str(amplitude_path),
    }
    return result


def _empty_summary(manifest_path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": "foot_structure_comparison",
        "manifest": str(manifest_path.expanduser().resolve()),
        "expected_variants": list(EXPECTED_VARIANTS),
        "cases": [
            {"case": case_id(surface, slope), "surface": surface, "slope_deg": slope}
            for surface, slope in EXPECTED_CASES
        ],
        "variants": {
            variant: {
                "cases": {
                    case_id(surface, slope): {
                        "status": "missing",
                        "error": "case not present in manifest",
                    }
                    for surface, slope in EXPECTED_CASES
                }
            }
            for variant in EXPECTED_VARIANTS
        },
    }


def _manifest_path(base: Path, value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Manifest field {key} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def generate_report(
    manifest_path: Path,
    output_dir: Path,
    *,
    dt_s: float | None = None,
    make_plots: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Analyze a manifest, write summary/selection JSON, and optionally draw plots."""
    manifest_path = manifest_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("variants"), dict):
        raise ValueError("Manifest must be an object containing a variants object")
    unknown_variants = set(manifest["variants"]) - set(EXPECTED_VARIANTS)
    if unknown_variants:
        raise ValueError(f"Unknown manifest variants: {sorted(unknown_variants)}")

    summary = _empty_summary(manifest_path)
    base = manifest_path.parent
    for variant in EXPECTED_VARIANTS:
        variant_spec = manifest["variants"].get(variant, {})
        if not isinstance(variant_spec, dict):
            raise ValueError(f"Manifest variant {variant} must be an object")
        expected_case_ids = {case_id(surface, slope) for surface, slope in EXPECTED_CASES}
        unknown_cases = set(variant_spec) - expected_case_ids
        if unknown_cases:
            raise ValueError(f"Unknown cases for {variant}: {sorted(unknown_cases)}")
        for surface, slope in EXPECTED_CASES:
            case = case_id(surface, slope)
            if case not in variant_spec:
                continue
            spec = variant_spec[case]
            if not isinstance(spec, dict):
                raise ValueError(f"Manifest scene {variant}/{case} must be an object")
            try:
                scene_dt = dt_s
                if scene_dt is None:
                    scene_dt = spec.get("dt_s", manifest.get("dt_s"))
                analysis = analyze_scene(
                    _manifest_path(base, spec.get("acceptance"), "acceptance"),
                    _manifest_path(base, spec.get("trace"), "trace"),
                    _manifest_path(base, spec.get("amplitude"), "amplitude"),
                    variant=variant,
                    surface=surface,
                    slope_deg=slope,
                    dt_s=scene_dt,
                )
                analysis_path = output_dir / "scenes" / variant / f"{case}.json"
                _write_json(analysis_path, analysis)
                summary["variants"][variant]["cases"][case] = {
                    "status": "ok",
                    "analysis_path": str(analysis_path),
                    "analysis": analysis,
                }
            except (OSError, ValueError, json.JSONDecodeError) as error:
                summary["variants"][variant]["cases"][case] = {
                    "status": "failed",
                    "error": str(error),
                }

    missing_cases = []
    failed_cases = []
    for variant in EXPECTED_VARIANTS:
        for surface, slope in EXPECTED_CASES:
            case = case_id(surface, slope)
            entry = summary["variants"][variant]["cases"][case]
            record = {"variant": variant, "case": case}
            if entry["status"] == "missing":
                missing_cases.append(record)
            elif entry["status"] == "failed":
                failed_cases.append({**record, "error": entry["error"]})
    summary["missing_cases"] = missing_cases
    summary["failed_cases"] = failed_cases
    summary["complete"] = not missing_cases and not failed_cases

    selection = select_variant(summary)
    plot_paths: dict[str, str] = {}
    if make_plots:
        plot_paths = create_plots(summary, output_dir)
    summary["plots"] = plot_paths
    summary_path = output_dir / "summary.json"
    selection_path = output_dir / "selection.json"
    _write_json(summary_path, summary)
    _write_json(selection_path, selection)
    return summary, selection


def _contains_nonfinite(value: Any) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif (
            isinstance(current, numbers.Real)
            and not isinstance(current, (bool, np.bool_))
            and not math.isfinite(float(current))
        ):
            return True
    return False


def _analysis_float(analysis: dict[str, Any], key: str) -> float | None:
    value = analysis.get(key)
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, numbers.Real):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _amplitude_value(analysis: dict[str, Any], name: str) -> float | None:
    amplitude = analysis.get("amplitude")
    if not isinstance(amplitude, dict):
        return None
    if name == "median_amplitude_ratio":
        return _analysis_float(amplitude, name)
    joints = amplitude.get("joints")
    if not isinstance(joints, dict) or not isinstance(joints.get(name), dict):
        return None
    return _analysis_float(joints[name], "amplitude_ratio")


def _valid_analysis(entry: dict[str, Any], prefix: str) -> tuple[dict[str, Any] | None, list[str]]:
    if entry.get("status") == "missing":
        return None, [f"{prefix}data_missing"]
    if entry.get("status") != "ok":
        return None, [f"{prefix}analysis_failed:{entry.get('error', 'unknown error')}"]
    analysis = entry.get("analysis")
    if not isinstance(analysis, dict):
        return None, [f"{prefix}analysis_missing"]
    reasons: list[str] = []
    if _contains_nonfinite(analysis):
        reasons.append(f"{prefix}nan_or_inf")
    if not analysis.get("data_complete", False):
        issues = analysis.get("data_issues") or ["unspecified"]
        reasons.extend(f"{prefix}data_incomplete:{issue}" for issue in issues)
    unexpected = analysis.get("unexpected_reset_count")
    if not isinstance(unexpected, numbers.Integral) or unexpected < 0:
        reasons.append(f"{prefix}unexpected_reset_count_missing")
    elif unexpected:
        reasons.append(f"{prefix}unexpected_resets:{unexpected}")
    return analysis, reasons


def _gate_case(
    candidate_entry: dict[str, Any], original_entry: dict[str, Any], *, baseline: bool
) -> dict[str, Any]:
    candidate, reasons = _valid_analysis(candidate_entry, "")
    if baseline:
        original = candidate
    else:
        original, original_reasons = _valid_analysis(original_entry, "original_")
        reasons.extend(original_reasons)
    result: dict[str, Any] = {
        "displacement_ratio": None,
        "slide_ratio_with_epsilon": None,
        "amplitude_checks": {},
    }
    if candidate is None or original is None:
        result["technical_gate_passed"] = False
        result["gate_failure_reasons"] = reasons
        return result

    displacement = _analysis_float(candidate, "root_planar_displacement_m")
    original_displacement = _analysis_float(original, "root_planar_displacement_m")
    slide = _analysis_float(candidate, "total_slide_distance_m")
    original_slide = _analysis_float(original, "total_slide_distance_m")
    if displacement is None or displacement < 0.0:
        reasons.append("displacement_missing_or_invalid")
    if original_displacement is None or original_displacement <= 0.0:
        reasons.append("original_displacement_missing_or_nonpositive")
    if slide is None or slide < 0.0:
        reasons.append("slide_missing_or_invalid")
    if original_slide is None or original_slide < 0.0:
        reasons.append("original_slide_missing_or_invalid")
    if (
        displacement is not None
        and original_displacement is not None
        and original_displacement > 0.0
    ):
        result["displacement_ratio"] = displacement / original_displacement
    if slide is not None and original_slide is not None:
        result["slide_ratio_with_epsilon"] = (slide + SLIDE_EPSILON_M) / (
            original_slide + SLIDE_EPSILON_M
        )

    amplitude_names = ("median_amplitude_ratio", *REQUIRED_JOINTS)
    for name in amplitude_names:
        value = _amplitude_value(candidate, name)
        original_value = _amplitude_value(original, name)
        check: dict[str, Any] = {
            "candidate": value,
            "original": original_value,
            "ratio_to_original": None,
            "minimum_ratio_to_original": AMPLITUDE_GATE,
            "passed": False,
        }
        if value is None:
            reasons.append(f"amplitude_missing:{name}")
        if original_value is None:
            reasons.append(f"original_amplitude_missing:{name}")
        if value is not None and original_value is not None:
            if original_value != 0.0:
                check["ratio_to_original"] = value / original_value
            check["passed"] = value >= AMPLITUDE_GATE * original_value
            if not check["passed"]:
                reasons.append(f"amplitude_gate:{name}")
        result["amplitude_checks"][name] = check

    result["technical_gate_passed"] = not reasons
    result["gate_failure_reasons"] = reasons
    return result


def select_variant(summary: dict[str, Any]) -> dict[str, Any]:
    """Apply technical gates and select the eligible non-baseline with minimum J."""
    variants_summary = summary.get("variants", {})
    original_cases = variants_summary.get("original", {}).get("cases", {})
    selection: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "selection": "foot_structure_variant",
        "parameters": {
            "amplitude_gate": AMPLITUDE_GATE,
            "slide_epsilon_m": SLIDE_EPSILON_M,
            "displacement_weight": DISPLACEMENT_WEIGHT,
            "slide_weight": SLIDE_WEIGHT,
            "flat_displacement_tolerance": FLAT_DISPLACEMENT_TOLERANCE,
            "expected_timeout_step": EXPECTED_TIMEOUT_STEP,
            "formula": "J=0.75*mean(Dv/Do)+0.25*mean((Sv+0.001)/(So+0.001))",
        },
        "original_excluded": True,
        "variants": {},
    }

    for variant in EXPECTED_VARIANTS:
        variant_cases = variants_summary.get(variant, {}).get("cases", {})
        case_results: dict[str, Any] = {}
        gate_reasons: list[str] = []
        for surface, slope in EXPECTED_CASES:
            case = case_id(surface, slope)
            missing_entry = {"status": "missing", "error": "case not present in summary"}
            candidate_entry = variant_cases.get(case, missing_entry)
            original_entry = original_cases.get(case, missing_entry)
            case_result = _gate_case(candidate_entry, original_entry, baseline=variant == "original")
            case_results[case] = case_result
            gate_reasons.extend(
                f"{case}:{reason}" for reason in case_result["gate_failure_reasons"]
            )
        gate_passed = not gate_reasons
        displacement_ratios = [
            case_results[case_id(surface, slope)]["displacement_ratio"]
            for surface, slope in EXPECTED_CASES
        ]
        slide_ratios = [
            case_results[case_id(surface, slope)]["slide_ratio_with_epsilon"]
            for surface, slope in EXPECTED_CASES
        ]
        score_j = None
        displacement_term = None
        slide_term = None
        if gate_passed and all(
            isinstance(value, numbers.Real) and math.isfinite(float(value))
            for value in (*displacement_ratios, *slide_ratios)
        ):
            displacement_term = DISPLACEMENT_WEIGHT * float(np.mean(displacement_ratios))
            slide_term = SLIDE_WEIGHT * float(np.mean(slide_ratios))
            score_j = displacement_term + slide_term

        eligibility_reasons: list[str] = []
        if variant == "original":
            eligibility_reasons.append("original_is_baseline_not_a_candidate")
        if not gate_passed:
            eligibility_reasons.append("technical_gate_failed")
        if score_j is not None and not score_j < 1.0:
            eligibility_reasons.append("j_not_less_than_1")
        for surface, slope in EXPECTED_CASES:
            case = case_id(surface, slope)
            ratio = case_results[case]["displacement_ratio"]
            if ratio is None:
                continue
            if slope == 4.0 and not ratio < 1.0:
                eligibility_reasons.append(f"{case}:displacement_not_better_than_original")
            if slope == 0.0 and ratio > FLAT_DISPLACEMENT_TOLERANCE:
                eligibility_reasons.append(f"{case}:displacement_regression_over_10_percent")
        eligible = not eligibility_reasons
        selection["variants"][variant] = {
            "technical_gate_passed": gate_passed,
            "gate_failure_reasons": gate_reasons,
            "cases": case_results,
            "displacement_ratio_mean": (
                float(np.mean(displacement_ratios)) if gate_passed and score_j is not None else None
            ),
            "slide_ratio_mean": (
                float(np.mean(slide_ratios)) if gate_passed and score_j is not None else None
            ),
            "displacement_term": displacement_term,
            "slide_term": slide_term,
            "score_j": score_j,
            "eligible": eligible,
            "eligibility_reasons": eligibility_reasons,
        }

    eligible_variants = [
        variant
        for variant in EXPECTED_VARIANTS
        if variant != "original" and selection["variants"][variant]["eligible"]
    ]
    winner = min(
        eligible_variants,
        key=lambda name: (selection["variants"][name]["score_j"], EXPECTED_VARIANTS.index(name)),
    ) if eligible_variants else None
    selection["winner"] = winner
    selection["winner_score_j"] = (
        selection["variants"][winner]["score_j"] if winner is not None else None
    )
    return selection


def _entry_metric(entry: dict[str, Any], metric: str) -> float:
    if entry.get("status") != "ok":
        return math.nan
    analysis = entry.get("analysis", {})
    if metric in REQUIRED_JOINTS:
        value = _amplitude_value(analysis, metric)
    elif metric == "median_amplitude_ratio":
        value = _amplitude_value(analysis, metric)
    else:
        value = _analysis_float(analysis, metric)
    return float(value) if value is not None else math.nan


def create_plots(summary: dict[str, Any], output_dir: Path) -> dict[str, str]:
    """Write English-labelled displacement, sliding, and amplitude comparison PNGs."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    labels = [f"{surface} {slope:g} deg" for surface, slope in EXPECTED_CASES]
    x = np.arange(len(EXPECTED_CASES))
    colors = plt.get_cmap("tab10").colors

    def plot_metric(path: Path, title: str, ylabel: str, metric: str) -> None:
        fig, ax = plt.subplots(figsize=(10, 6))
        has_data = False
        for index, variant in enumerate(EXPECTED_VARIANTS):
            cases = summary["variants"][variant]["cases"]
            values = np.asarray(
                [_entry_metric(cases[case_id(surface, slope)], metric) for surface, slope in EXPECTED_CASES],
                dtype=np.float64,
            )
            has_data = has_data or bool(np.isfinite(values).any())
            ax.plot(
                x,
                values,
                marker="o",
                linewidth=1.8,
                color=colors[index % len(colors)],
                label=variant,
            )
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, labels)
        ax.grid(axis="y", alpha=0.25)
        ax.legend(loc="best", ncols=2)
        if not has_data:
            ax.text(0.5, 0.5, "No valid scene data", transform=ax.transAxes, ha="center")
        fig.text(
            0.01,
            0.01,
            "Missing/failed points are omitted (not zero); see summary.json.",
            fontsize=9,
        )
        fig.tight_layout(rect=(0.0, 0.04, 1.0, 1.0))
        fig.savefig(path, dpi=150)
        plt.close(fig)

    paths = {
        "displacement": output_dir / "displacement.png",
        "sliding": output_dir / "sliding.png",
        "amplitude": output_dir / "amplitude.png",
    }
    plot_metric(
        paths["displacement"],
        "Root planar displacement",
        "D (m)",
        "root_planar_displacement_m",
    )
    plot_metric(
        paths["sliding"],
        "Cumulative two-foot sliding",
        "S (m)",
        "total_slide_distance_m",
    )

    fig, axes = plt.subplots(3, 1, figsize=(10, 13), sharex=True)
    amplitude_metrics = (
        ("median_amplitude_ratio", "Median amplitude ratio"),
        ("right_knee", "Right knee amplitude ratio"),
        ("right_ankle", "Right ankle amplitude ratio"),
    )
    for ax, (metric, title) in zip(axes, amplitude_metrics, strict=True):
        for index, variant in enumerate(EXPECTED_VARIANTS):
            cases = summary["variants"][variant]["cases"]
            values = np.asarray(
                [_entry_metric(cases[case_id(surface, slope)], metric) for surface, slope in EXPECTED_CASES],
                dtype=np.float64,
            )
            ax.plot(
                x,
                values,
                marker="o",
                linewidth=1.6,
                color=colors[index % len(colors)],
                label=variant,
            )
        ax.set_title(title)
        ax.set_ylabel("Ratio")
        ax.grid(axis="y", alpha=0.25)
    axes[-1].set_xticks(x, labels)
    axes[0].legend(loc="best", ncols=2)
    fig.text(
        0.01,
        0.01,
        "Missing/failed points are omitted (not zero); see summary.json.",
        fontsize=9,
    )
    fig.tight_layout(rect=(0.0, 0.025, 1.0, 1.0))
    fig.savefig(paths["amplitude"], dpi=150)
    plt.close(fig)
    return {name: str(path) for name, path in paths.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze one Isaac scene")
    analyze_parser.add_argument("--acceptance", type=Path, required=True)
    analyze_parser.add_argument("--trace", type=Path, required=True)
    analyze_parser.add_argument("--amplitude", type=Path, required=True)
    analyze_parser.add_argument("--output", type=Path, required=True)
    analyze_parser.add_argument("--variant", default="original")
    analyze_parser.add_argument("--surface", choices=("normal", "marble"), required=True)
    analyze_parser.add_argument("--slope-deg", type=float, default=None)
    analyze_parser.add_argument("--dt-s", "--dt", dest="dt_s", type=float, default=None)

    report_parser = subparsers.add_parser("report", help="Analyze all cases, plot, and select")
    report_parser.add_argument("--manifest", type=Path, required=True)
    report_parser.add_argument("--output-dir", type=Path, required=True)
    report_parser.add_argument("--dt-s", "--dt", dest="dt_s", type=float, default=None)
    args = parser.parse_args()

    try:
        if args.command == "analyze":
            acceptance = _load_json(args.acceptance)
            slope = args.slope_deg
            if slope is None:
                slope = acceptance.get("terrain", {}).get("slope_deg")
                if slope is None:
                    raise ValueError("Pass --slope-deg or provide acceptance.terrain.slope_deg")
            report = analyze_scene_data(
                acceptance,
                _load_json(args.trace),
                _load_json(args.amplitude),
                variant=args.variant,
                surface=args.surface,
                slope_deg=slope,
                dt_s=args.dt_s,
            )
            report["source"] = {
                "acceptance": str(args.acceptance.expanduser().resolve()),
                "trace": str(args.trace.expanduser().resolve()),
                "amplitude": str(args.amplitude.expanduser().resolve()),
            }
            _write_json(args.output, report)
            print(json.dumps(report, indent=2, allow_nan=False))
        else:
            summary, selection = generate_report(
                args.manifest, args.output_dir, dt_s=args.dt_s
            )
            print(
                json.dumps(
                    {
                        "summary": str((args.output_dir / "summary.json").resolve()),
                        "selection": str((args.output_dir / "selection.json").resolve()),
                        "plots": summary["plots"],
                        "complete": summary["complete"],
                        "missing_case_count": len(summary["missing_cases"]),
                        "failed_case_count": len(summary["failed_cases"]),
                        "winner": selection["winner"],
                        "winner_score_j": selection["winner_score_j"],
                    },
                    indent=2,
                    allow_nan=False,
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
