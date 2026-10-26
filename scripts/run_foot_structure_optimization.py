#!/usr/bin/env python3
"""Run the resumable V5 foot-structure optimization pipeline."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any

import mujoco
import numpy as np
import onnxruntime as ort

import foot_structure_ledger


ROOT = Path(__file__).resolve().parents[1]
RL = ROOT / "work/dance/microduck_rl"
VIDEO_DANCE = ROOT / "work/video_dance"
WORK = VIDEO_DANCE / "v5_foot_structure"
ARTIFACTS = ROOT / "artifacts/isaac/video-dance-foot-v5"
VARIANT_MATRIX = WORK / "variant_matrix.json"
TASK = "Mjlab-DanceClip-FootStructure-MicroDuck"
EXPERIMENT = "microduck_dance_foot_structure"
CLIP = VIDEO_DANCE / "outputs/studio_side_step_06_10_v2/duck_motion.npz"
PARENT_RUN = "2026-10-18_11-30-08_microduck_dance_hard_floor"
PARENT_CHECKPOINT = "model_998.pt"
PARENT_DIR = RL / "logs/rsl_rl/microduck_dance_hard_floor" / PARENT_RUN
VARIANTS = ("original", "W090", "W115", "W130", "L110", "L110W115", "L110W130")
CASES = (
    ("normal", 0, 1.0),
    ("normal", 4, 1.0),
    ("marble", 0, 0.55),
    ("marble", 4, 0.55),
)
REQUIRED_TRACE_FIELDS = {
    "step",
    "dt_s",
    "done",
    "root_position",
    "joint_position",
    "foot_position",
    "foot_linear_velocity",
    "foot_force",
    "foot_loaded",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def git_revision(path: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True
    ).strip()


def training_metrics(log_path: Path) -> dict[str, float]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "mean_reward": r"Mean reward:\s*(-?[0-9.]+)",
        "mean_episode_length": r"Mean episode length:\s*(-?[0-9.]+)",
        "dance_pose_track": r"Episode_Reward/dance_pose_track:\s*(-?[0-9.]+)",
        "dance_feet_schedule": r"Episode_Reward/dance_feet_schedule:\s*(-?[0-9.]+)",
        "fell_over": r"Episode_Termination/fell_over:\s*(-?[0-9.]+)",
        "nan_state": r"Episode_Termination/nan_state:\s*(-?[0-9.]+)",
    }
    result: dict[str, float] = {}
    for name, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if not matches:
            raise ValueError(f"Missing {name} in {log_path}")
        result[name] = float(matches[-1])
    return result


class Pipeline:
    def __init__(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.manifest_path = WORK / "manifest.json"
        self.events_path = WORK / "events.jsonl"
        if self.manifest_path.is_file():
            self.manifest = read_json(self.manifest_path)
        else:
            self.manifest = {
                "schema_version": 1,
                "created_utc": utc_now(),
                "task": TASK,
                "variants": {name: {"stages": {}} for name in VARIANTS},
                "parent": {
                    "run_dir": str(PARENT_DIR),
                    "checkpoint": PARENT_CHECKPOINT,
                    "sha256": sha256(PARENT_DIR / PARENT_CHECKPOINT),
                },
                "clip": {"path": str(CLIP), "sha256": sha256(CLIP)},
                "variant_matrix": {
                    "path": str(VARIANT_MATRIX),
                    "sha256": sha256(VARIANT_MATRIX),
                },
                "git": {
                    "project": git_revision(ROOT),
                    "microduck_rl": git_revision(RL),
                },
            }
            self.save()

    def save(self) -> None:
        self.manifest["updated_utc"] = utc_now()
        write_json(self.manifest_path, self.manifest)

    def record(self, variant: str, stage: str, status: str, **fields: Any) -> None:
        event = {
            "variant": variant,
            "stage": stage,
            "status": status,
            "timestamp_utc": utc_now(),
            **fields,
        }
        event = foot_structure_ledger.append_event(self.events_path, event)
        stages = self.manifest["variants"].setdefault(variant, {}).setdefault("stages", {})
        stages[stage] = event
        self.save()
        print(f"V5_EVENT {variant} {stage} {status}", flush=True)

    def stage(self, variant: str, stage: str) -> dict[str, Any] | None:
        return self.manifest["variants"].get(variant, {}).get("stages", {}).get(stage)

    def stage_ok(self, variant: str, stage: str) -> bool:
        event = self.stage(variant, stage)
        return bool(event and event.get("status") == "success")

    def execute(
        self,
        variant: str,
        stage: str,
        command: list[str],
        *,
        cwd: Path,
        log_path: Path,
        env_overrides: dict[str, str] | None = None,
        timeout_s: int | None = None,
        on_success: Any = None,
        resume: bool = True,
    ) -> bool:
        if resume and self.stage_ok(variant, stage):
            return True
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        started = time.monotonic()
        started_utc = utc_now()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.record(
            variant,
            stage,
            "running",
            command=command,
            cwd=str(cwd),
            env_overrides=env_overrides or {},
            started_utc=started_utc,
            log=str(log_path),
        )
        return_code: int | None = None
        timed_out = False
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return_code = process.returncode
        metadata: dict[str, Any] = {}
        error: str | None = None
        if return_code == 0 and not timed_out and on_success is not None:
            try:
                metadata = on_success() or {}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        status = "success" if return_code == 0 and not timed_out and error is None else "failed"
        fields: dict[str, Any] = {
            "command": command,
            "cwd": str(cwd),
            "env_overrides": env_overrides or {},
            "started_utc": started_utc,
            "finished_utc": utc_now(),
            "elapsed_s": time.monotonic() - started,
            "exit_code": return_code,
            "timed_out": timed_out,
            "log": str(log_path),
            **metadata,
        }
        if status == "failed":
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            if error:
                tail += f"\n{error}\n"
            error_path = log_path.with_suffix(log_path.suffix + ".error.txt")
            error_path.write_text(tail, encoding="utf-8")
            fields["error_tail"] = str(error_path)
            if error:
                fields["error"] = error
        self.record(variant, stage, status, **fields)
        return status == "success"

    def skip(self, variant: str, stage: str, reason: str) -> None:
        self.record(variant, stage, "skipped", reason=reason)

    def ensure_parent_link(self) -> None:
        target_root = RL / "logs/rsl_rl" / EXPERIMENT
        target_root.mkdir(parents=True, exist_ok=True)
        link = target_root / PARENT_RUN
        expected = Path("..") / "microduck_dance_hard_floor" / PARENT_RUN
        if link.is_symlink():
            if Path(os.readlink(link)) != expected:
                raise RuntimeError(f"Unexpected parent link: {link} -> {os.readlink(link)}")
        elif link.exists():
            if not link.is_dir() or (link / PARENT_CHECKPOINT).resolve() != (PARENT_DIR / PARENT_CHECKPOINT).resolve():
                raise RuntimeError(f"Parent path is not the expected run: {link}")
        else:
            link.symlink_to(expected, target_is_directory=True)

    def variant_xml(self, variant: str) -> Path:
        return WORK / "variants" / variant / "robot_walk.xml"

    def variant_usd(self, variant: str) -> Path:
        return ARTIFACTS / "usd" / variant / "robot_groundcontact/robot_groundcontact.usda"

    def convert(self, variant: str) -> bool:
        stage = "convert"
        usd = self.variant_usd(variant)
        if self.stage_ok(variant, stage) and usd.is_file():
            return True
        source = WORK / "variants" / variant / "robot_groundcontact.xml"
        output_dir = usd.parent
        evidence = ARTIFACTS / "usd" / variant / "evidence"
        expected_mass = float(mujoco.MjModel.from_xml_path(str(source)).body_mass.sum())

        def validate() -> dict[str, Any]:
            inventory = read_json(evidence / "usd_inventory.json")
            failed = [name for name, passed in inventory["checks"].items() if not passed]
            if failed:
                raise RuntimeError(f"USD checks failed: {failed}")
            if not usd.is_file():
                raise FileNotFoundError(usd)
            return {
                "usd": str(usd),
                "usd_sha256": sha256(usd),
                "expected_mass_kg": expected_mass,
                "inventory": str(evidence / "usd_inventory.json"),
            }

        return self.execute(
            variant,
            stage,
            [
                "bash",
                str(ROOT / "scripts/convert_mjcf_to_usd.sh"),
                "--variant",
                "--expected-mass-kg",
                f"{expected_mass:.17g}",
            ],
            cwd=ROOT,
            log_path=ARTIFACTS / "logs" / f"{variant}-convert.log",
            env_overrides={
                "MICRODUCK_MJCF_INPUT": str(source),
                "MICRODUCK_USD_OUTPUT_DIR": str(output_dir),
                "MICRODUCK_USD_EVIDENCE_DIR": str(evidence),
                "ISAACLAB_DIR": str(Path.home() / "rlgpu_ws/IsaacLab"),
            },
            timeout_s=600,
            on_success=validate,
        )

    def validate_onnx(self, path: Path) -> dict[str, Any]:
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        input_shape = list(session.get_inputs()[0].shape)
        output = session.run(
            None, {session.get_inputs()[0].name: np.zeros((1, 61), np.float32)}
        )[0]
        if input_shape != [1, 61] or output.shape != (1, 14) or not np.isfinite(output).all():
            raise RuntimeError(f"Invalid ONNX contract: input={input_shape}, output={output.shape}")
        return {"onnx": str(path), "onnx_sha256": sha256(path), "onnx_contract": "[1,61]->(1,14)"}

    def latest_checkpoint(self, run_dir: Path) -> Path:
        candidates = []
        for path in run_dir.glob("model_*.pt"):
            match = re.fullmatch(r"model_(\d+)\.pt", path.name)
            if match:
                candidates.append((int(match.group(1)), path))
        if not candidates:
            raise FileNotFoundError(f"No numbered checkpoint in {run_dir}")
        return max(candidates)[1]

    def validate_video(self, video: Path, review: Path) -> dict[str, Any]:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
                "-of",
                "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(probe.stdout)
        frames = int(metadata["streams"][0]["nb_frames"])
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"],
            check=True,
            capture_output=True,
        )
        indices = np.linspace(0, frames - 1, 5).round().astype(int)
        selector = "+".join(f"eq(n,{index})" for index in indices)
        width, height = (320, 240) if frames <= 500 else (384, 216)
        review.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-vf",
                f"select='{selector}',scale={width}:{height},tile=5x1",
                "-frames:v",
                "1",
                str(review),
            ],
            check=True,
            capture_output=True,
        )
        return {
            "video": str(video),
            "video_sha256": sha256(video),
            "video_frames": frames,
            "video_probe": metadata,
            "review_image": str(review),
        }

    def training_media(self, variant: str, seed: int, smoke: bool, run_dir: Path) -> bool:
        label = "smoke" if smoke else "train"
        stage = f"media-{label}-seed{seed}"
        if self.stage_ok(variant, stage):
            return True
        video_dir = run_dir / "videos/train"
        videos = list(video_dir.glob("rl-video-step-*.mp4"))
        if not videos:
            self.record(variant, stage, "failed", error=f"No training videos in {video_dir}")
            return False
        chosen = {min(videos, key=lambda p: int(re.search(r"step-(\d+)", p.name).group(1)))}
        chosen.add(max(videos, key=lambda p: int(re.search(r"step-(\d+)", p.name).group(1))))
        records = []
        try:
            for video in sorted(chosen):
                step = int(re.search(r"step-(\d+)", video.name).group(1))
                review = ARTIFACTS / "review" / f"seed{seed}" / variant / f"{label}-step-{step}.jpg"
                records.append(self.validate_video(video, review))
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
            self.record(variant, stage, "failed", error=str(exc))
            return False
        self.record(variant, stage, "success", media=records)
        return True

    def train(self, variant: str, seed: int, smoke: bool) -> bool:
        label = "smoke" if smoke else "train"
        stage = f"{label}-seed{seed}"
        if smoke:
            iterations, envs, timeout = 5, 64, 300
        else:
            iterations, envs, timeout = 500, 1024, 2400
        self.ensure_parent_link()
        log_root = RL / "logs/rsl_rl" / EXPERIMENT
        before = set(log_root.glob("2026*"))
        run_dir: dict[str, Path] = {}

        def validate() -> dict[str, Any]:
            new = set(log_root.glob("2026*")) - before
            if len(new) != 1:
                raise RuntimeError(f"Expected one new run directory, got {sorted(map(str, new))}")
            run_dir["path"] = new.pop()
            checkpoint = self.latest_checkpoint(run_dir["path"])
            return {
                "run_dir": str(run_dir["path"]),
                "checkpoint": str(checkpoint),
                "checkpoint_sha256": sha256(checkpoint),
                "seed": seed,
                "iterations": iterations,
                "num_envs": envs,
                "metrics": training_metrics(
                    ARTIFACTS / "logs" / f"{variant}-{label}-seed{seed}.log"
                ),
            }

        ok = self.execute(
            variant,
            stage,
            [
                "uv",
                "run",
                "--no-sync",
                "train",
                TASK,
                "--env.scene.num-envs",
                str(envs),
                "--agent.max_iterations",
                str(iterations),
                "--agent.seed",
                str(seed),
                "--agent.resume",
                "True",
                "--agent.load-run",
                PARENT_RUN,
                "--agent.load-checkpoint",
                PARENT_CHECKPOINT,
                "--video",
                "True",
                "--video-interval",
                "2000",
                "--video-length",
                "500",
            ],
            cwd=RL,
            log_path=ARTIFACTS / "logs" / f"{variant}-{label}-seed{seed}.log",
            env_overrides={
                "WANDB_MODE": "offline",
                "MICRODUCK_DANCE_CLIP": str(CLIP),
                "MICRODUCK_DANCE_MAX_SLOPE_DEG": "4",
                "MICRODUCK_WARM_START": "1",
                "MICRODUCK_WALK_XML": str(self.variant_xml(variant)),
            },
            timeout_s=timeout,
            on_success=validate,
        )
        if not ok:
            return False
        checkpoint = Path(self.stage(variant, stage)["checkpoint"])
        export_stage = f"export-{label}-seed{seed}"
        onnx = ARTIFACTS / "policies" / f"seed{seed}" / variant / f"{label}_policy.onnx"
        onnx.parent.mkdir(parents=True, exist_ok=True)
        ok = self.execute(
            variant,
            export_stage,
            [
                "uv",
                "run",
                "--no-sync",
                "scripts/export.py",
                TASK,
                "--checkpoint-file",
                str(checkpoint),
                "--onnx-file",
                str(onnx),
            ],
            cwd=RL,
            log_path=ARTIFACTS / "logs" / f"{variant}-export-{label}-seed{seed}.log",
            env_overrides={
                "MICRODUCK_DANCE_CLIP": str(CLIP),
                "MICRODUCK_DANCE_MAX_SLOPE_DEG": "4",
                "MICRODUCK_WARM_START": "0",
                "MICRODUCK_WALK_XML": str(self.variant_xml(variant)),
            },
            timeout_s=120,
            on_success=lambda: self.validate_onnx(onnx),
        )
        if not ok:
            return False
        return self.training_media(variant, seed, smoke, Path(self.stage(variant, stage)["run_dir"]))

    def accept_case(self, variant: str, seed: int, surface: str, slope: int, friction: float) -> bool:
        case = f"{surface}-slope-{slope}"
        stage = f"accept-seed{seed}-{case}"
        output_dir = ARTIFACTS / "acceptance" / f"seed{seed}" / variant
        acceptance = output_dir / f"acceptance-{case}.json"
        trace = output_dir / f"trace-{case}.json"
        video = output_dir / f"acceptance-{case}.mp4"
        screenshot = output_dir / f"acceptance-{case}.png"
        review = ARTIFACTS / "review" / f"seed{seed}" / variant / f"acceptance-{case}.jpg"
        onnx = ARTIFACTS / "policies" / f"seed{seed}" / variant / "train_policy.onnx"

        def validate() -> dict[str, Any]:
            summary = read_json(acceptance)
            rows = read_json(trace)
            if len(rows) != 1600 or not REQUIRED_TRACE_FIELDS.issubset(rows[0]):
                raise RuntimeError("Incomplete 1600-step foot trace")
            if summary.get("robot_usd") != str(self.variant_usd(variant).resolve()):
                raise RuntimeError("Playback used an unexpected robot USD")
            media = self.validate_video(video, review)
            return {
                "acceptance": str(acceptance),
                "trace": str(trace),
                "screenshot": str(screenshot),
                "done_count": summary["done_count"],
                "root_displacement_m": summary["mean_root_displacement_m"],
                **media,
            }

        ok = self.execute(
            variant,
            stage,
            [
                "bash",
                str(ROOT / "scripts/play_isaac_velocity.sh"),
                "--profile",
                "baseline",
                "--viz",
                "none",
                "--policy-onnx",
                str(onnx),
                "--robot-usd",
                str(self.variant_usd(variant)),
                "--phase-clock",
                "4.966666666666667",
                "--reset-root-height",
                "0.12",
                "--slope-deg",
                str(slope),
                "--slope-azimuth-deg",
                "0",
                "--terrain-friction",
                str(friction),
                "--terrain-roughness",
                "0",
                "--video",
                str(video),
                "--trace",
                str(trace),
                "--seed",
                str(seed),
            ],
            cwd=ROOT,
            log_path=ARTIFACTS / "logs" / f"{variant}-accept-seed{seed}-{case}.log",
            env_overrides={
                "ISAACLAB_DIR": str(Path.home() / "rlgpu_ws/IsaacLab"),
                "PYTHONPATH": str(ROOT / "work/isaac_python_pkgs"),
                "MICRODUCK_PLAY_STEPS": "1600",
                "MICRODUCK_PLAY_TIMEOUT": "300",
                "MICRODUCK_PLAY_OUTPUT": str(acceptance),
                "MICRODUCK_PLAY_SCREENSHOT": str(screenshot),
            },
            timeout_s=330,
            on_success=validate,
        )
        if not ok:
            return False
        amplitude = ARTIFACTS / "analysis" / f"seed{seed}" / variant / f"amplitude-{case}.json"
        return self.execute(
            variant,
            f"amplitude-seed{seed}-{case}",
            [
                str(VIDEO_DANCE / ".venv/bin/python"),
                "-m",
                "video_dance.analyze_isaac_amplitude",
                "--duck-motion",
                "outputs/studio_side_step_06_10_v2/duck_motion.npz",
                "--acceptance",
                str(acceptance),
                "--trace",
                str(trace),
                "--output",
                str(amplitude),
            ],
            cwd=VIDEO_DANCE,
            log_path=ARTIFACTS / "logs" / f"{variant}-amplitude-seed{seed}-{case}.log",
            timeout_s=120,
            on_success=lambda: {"amplitude": str(amplitude), "amplitude_sha256": sha256(amplitude)},
        )

    def accept(self, variant: str, seed: int) -> bool:
        training_event = self.stage(variant, f"train-seed{seed}")
        metrics = training_event.get("metrics", {}) if training_event else {}
        baseline_event = self.stage("original", f"train-seed{seed}")
        baseline_metrics = baseline_event.get("metrics", {}) if baseline_event else {}
        baseline_fell_over = baseline_metrics.get("fell_over")
        rules = [
            {
                "rule_id": "nan_state",
                "threshold": 0.0,
                "observed": metrics.get("nan_state"),
                "passed": metrics.get("nan_state", 1.0) == 0.0,
            },
            {
                "rule_id": "fell_over_absolute",
                "threshold": 0.375,
                "observed": metrics.get("fell_over"),
                "passed": metrics.get("fell_over", math.inf) <= 0.375,
            },
        ]
        if variant != "original":
            rules.append(
                {
                    "rule_id": "fell_over_regression",
                    "threshold": baseline_fell_over + 0.05 if baseline_fell_over is not None else None,
                    "observed": metrics.get("fell_over"),
                    "passed": baseline_fell_over is not None
                    and metrics.get("fell_over", math.inf) <= baseline_fell_over + 0.05,
                }
            )
        gate_reasons = []
        if metrics.get("nan_state", 1.0) != 0.0:
            gate_reasons.append("nan_state_nonzero")
        if metrics.get("fell_over", math.inf) > 0.375:
            gate_reasons.append("fell_over_above_0.375")
        if variant != "original" and metrics.get("fell_over", math.inf) > (
            baseline_metrics.get("fell_over", -math.inf) + 0.05
        ):
            gate_reasons.append("fell_over_regression_over_0.05")
        self.record(
            variant,
            f"gate-training-seed{seed}",
            "success" if not gate_reasons else "failed",
            rules=rules,
            passed=not gate_reasons,
        )
        if gate_reasons:
            self.record(
                variant,
                f"accept-seed{seed}",
                "failed",
                reason="training_gate_failed",
                gate_reasons=gate_reasons,
                training_metrics=metrics,
                baseline_training_metrics=baseline_metrics,
            )
            return False
        success = True
        for surface, slope, friction in CASES:
            if not self.accept_case(variant, seed, surface, slope, friction):
                success = False
        self.record(
            variant,
            f"accept-seed{seed}",
            "success" if success else "failed",
            cases=[f"{surface}-slope-{slope}" for surface, slope, _ in CASES],
            training_metrics=metrics,
            baseline_training_metrics=baseline_metrics,
        )
        return success

    def analysis_manifest(self, seed: int) -> Path:
        path = ARTIFACTS / "analysis" / f"seed{seed}" / "input_manifest.json"
        variants: dict[str, Any] = {}
        for variant in VARIANTS:
            variants[variant] = {}
            for surface, slope, _ in CASES:
                case = f"{surface}-slope-{slope}"
                acceptance_dir = ARTIFACTS / "acceptance" / f"seed{seed}" / variant
                variants[variant][case] = {
                    "acceptance": str(acceptance_dir / f"acceptance-{case}.json"),
                    "trace": str(acceptance_dir / f"trace-{case}.json"),
                    "amplitude": str(ARTIFACTS / "analysis" / f"seed{seed}" / variant / f"amplitude-{case}.json"),
                }
        write_json(path, {"dt_s": 0.02, "variants": variants})
        return path

    def analyze(self, seed: int) -> dict[str, Any] | None:
        output_dir = ARTIFACTS / "analysis" / f"seed{seed}"
        ok = self.execute(
            "original",
            f"analyze-seed{seed}",
            [
                sys.executable,
                str(ROOT / "scripts/analyze_foot_structure.py"),
                "report",
                "--manifest",
                str(self.analysis_manifest(seed)),
                "--output-dir",
                str(output_dir),
            ],
            cwd=ROOT,
            log_path=ARTIFACTS / "logs" / f"analyze-seed{seed}.log",
            timeout_s=180,
            on_success=lambda: {
                "summary": str(output_dir / "summary.json"),
                "selection": str(output_dir / "selection.json"),
                "summary_sha256": sha256(output_dir / "summary.json"),
                "selection_sha256": sha256(output_dir / "selection.json"),
            },
            resume=False,
        )
        if not ok:
            return None
        selection_path = output_dir / "selection.json"
        selection = read_json(selection_path)
        self.record(
            "original",
            f"decision-selection-seed{seed}",
            "success",
            winner=selection.get("winner"),
            eligible=[
                name
                for name in VARIANTS
                if selection["variants"].get(name, {}).get("eligible")
            ],
            gates={
                name: {
                    "technical_gate_passed": record.get("technical_gate_passed"),
                    "gate_failure_reasons": record.get("gate_failure_reasons"),
                    "score_j": record.get("score_j"),
                    "eligible": record.get("eligible"),
                    "eligibility_reasons": record.get("eligibility_reasons"),
                }
                for name, record in selection["variants"].items()
            },
            parameters=selection.get("parameters"),
            selection_sha256=sha256(selection_path),
        )
        return selection

    def confirmation_verdict(self, seed: int, winner: str) -> dict[str, Any]:
        summary = read_json(ARTIFACTS / "analysis" / f"seed{seed}/summary.json")
        checks: dict[str, Any] = {}
        reasons: list[str] = []
        for surface, slope, _ in CASES:
            case = f"{surface}-slope-{slope}"
            candidate_entry = summary["variants"][winner]["cases"][case]
            original_entry = summary["variants"]["original"]["cases"][case]
            case_reasons: list[str] = []
            if candidate_entry.get("status") != "ok" or original_entry.get("status") != "ok":
                case_reasons.append("missing_or_failed_analysis")
                checks[case] = {"passed": False, "reasons": case_reasons}
                reasons.extend(f"{case}:{reason}" for reason in case_reasons)
                continue
            candidate = candidate_entry["analysis"]
            original = original_entry["analysis"]
            if not candidate.get("data_complete") or not original.get("data_complete"):
                case_reasons.append("incomplete_analysis_data")
            ratio = candidate["root_planar_displacement_m"] / original["root_planar_displacement_m"]
            if slope == 4:
                if not ratio <= 0.9:
                    case_reasons.append("displacement_reduction_below_10_percent")
                if not candidate["root_planar_displacement_m"] < 0.25:
                    case_reasons.append("displacement_not_below_0.25m")
            elif ratio > 1.1:
                case_reasons.append("flat_displacement_regression_over_10_percent")
            for name in ("median_amplitude_ratio", "right_knee", "right_ankle"):
                if name == "median_amplitude_ratio":
                    value = candidate["amplitude"][name]
                    baseline = original["amplitude"][name]
                else:
                    value = candidate["amplitude"]["joints"][name]["amplitude_ratio"]
                    baseline = original["amplitude"]["joints"][name]["amplitude_ratio"]
                if value < 0.95 * baseline:
                    case_reasons.append(f"amplitude_gate:{name}")
            if candidate["unexpected_reset_count"] or original["unexpected_reset_count"]:
                case_reasons.append("unexpected_reset")
            checks[case] = {
                "passed": not case_reasons,
                "reasons": case_reasons,
                "displacement_ratio": ratio,
                "candidate_displacement_m": candidate["root_planar_displacement_m"],
                "original_displacement_m": original["root_planar_displacement_m"],
                "candidate_slide_m": candidate["total_slide_distance_m"],
                "original_slide_m": original["total_slide_distance_m"],
            }
            reasons.extend(f"{case}:{reason}" for reason in case_reasons)
        verdict = {
            "winner": winner,
            "seed": seed,
            "passed": not reasons,
            "status": "improvement_confirmed" if not reasons else "confirmation_failed",
            "reasons": reasons,
            "cases": checks,
        }
        write_json(ARTIFACTS / "analysis" / f"seed{seed}/confirmation_verdict.json", verdict)
        self.record(winner, f"confirmation-seed{seed}", "success" if not reasons else "failed", verdict=verdict)
        return verdict

    def pipeline(self) -> None:
        for variant in VARIANTS:
            self.convert(variant)
        for variant in VARIANTS:
            if not self.stage_ok(variant, "convert"):
                self.skip(variant, "smoke-seed42", "convert_failed")
            else:
                self.train(variant, 42, smoke=True)
        for variant in VARIANTS:
            smoke_gates = ("smoke-seed42", "export-smoke-seed42", "media-smoke-seed42")
            if not all(self.stage_ok(variant, stage) for stage in smoke_gates):
                self.skip(variant, "train-seed42", "smoke_failed")
            else:
                self.train(variant, 42, smoke=False)
        for variant in VARIANTS:
            train_gates = ("train-seed42", "export-train-seed42", "media-train-seed42")
            if not all(self.stage_ok(variant, stage) for stage in train_gates):
                self.skip(variant, "accept-seed42", "training_or_export_failed")
            else:
                self.accept(variant, 42)
        selection = self.analyze(42)
        if selection is None:
            return
        winner = selection.get("winner")
        if winner is None:
            verdict = {
                "passed": False,
                "status": "no_eligible_variant",
                "reason": "No variant passed the seed-42 gates",
            }
            write_json(ARTIFACTS / "analysis/seed42/final_verdict.json", verdict)
            self.record("original", "optimization", "failed", verdict=verdict)
            self.record("original", "decision-final", "failed", winner=None, verdict=verdict)
            return
        for variant in ("original", winner):
            self.train(variant, 43, smoke=False)
        for variant in ("original", winner):
            confirmation_gates = ("train-seed43", "export-train-seed43", "media-train-seed43")
            if all(self.stage_ok(variant, stage) for stage in confirmation_gates):
                self.accept(variant, 43)
            else:
                self.skip(variant, "accept-seed43", "confirmation_training_failed")
        if self.analyze(43) is not None:
            verdict = self.confirmation_verdict(43, winner)
            self.record(
                "original",
                "decision-final",
                "success" if verdict["passed"] else "failed",
                winner=winner,
                verdict=verdict,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("convert", "smoke", "train", "accept"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
        if name in ("train", "accept"):
            sub.add_argument("--seed", type=int, default=42)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--seed", type=int, default=42)
    subparsers.add_parser("pipeline")
    args = parser.parse_args()
    pipeline = Pipeline()
    if args.command == "pipeline":
        pipeline.pipeline()
    elif args.command == "convert":
        for variant in args.variants:
            pipeline.convert(variant)
    elif args.command == "smoke":
        for variant in args.variants:
            pipeline.train(variant, 42, smoke=True)
    elif args.command == "train":
        for variant in args.variants:
            pipeline.train(variant, args.seed, smoke=False)
    elif args.command == "accept":
        for variant in args.variants:
            pipeline.accept(variant, args.seed)
    else:
        selection = pipeline.analyze(args.seed)
        print(json.dumps(selection, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
