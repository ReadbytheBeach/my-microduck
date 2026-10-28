#!/usr/bin/env python3
"""Adapters: bind skills to controlled launch templates.

Every adapter builds an explicit LaunchTemplate (interpreter, argv, cwd,
minimal env, timeouts, expected outputs). The program chooses templates;
the LLM never emits paths, task names, or shell fragments. M0 adapters:
isaac_playback (velocity / phase / posture_flag / constant encodings) and
mujoco_baseline (velocity only). Default is dry-run: the template is
constructed but not started unless execute=True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISAACLAB_SH = Path(
    os.environ.get("ISAACLAB_DIR", str(Path.home() / "rlgpu_ws/IsaacLab"))
) / "isaaclab.sh"
ISAAC_KIT_ARGS = (
    "--/renderer/multiGpu/autoEnable=0 --/renderer/multiGpu/enabled=0 "
    "--/renderer/activeGpu=0 --/crashreporter/skipOldDumpUpload=1 "
    "--/crashreporter/preserveDump=1"
)
MUJOCO_PYTHON = PROJECT_ROOT / "work/dance/microduck_rl/.venv/bin/python"
CONTROL_HZ = 50.0
CONTROL_DT_S = 1.0 / CONTROL_HZ

# Subprocesses get an explicit minimal environment: a small allowlist of
# inherited names plus the ones the template sets itself. Anything that
# looks like a credential is never inherited.
_ENV_ALLOWLIST = (
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TERM",
    "XDG_RUNTIME_DIR",
    "VK_DRIVER_FILES",
    "VK_ICD_FILENAMES",
)
_SECRET_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_CONDA_RE = re.compile(r"(miniconda|anaconda|\.conda)")


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # conda on PATH crashes Isaac's bundled Python (SRE module mismatch).
    kept = [entry for entry in path.split(os.pathsep) if not _CONDA_RE.search(entry)]
    env["PATH"] = os.pathsep.join(kept)
    for name in _ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value and not _SECRET_RE.search(name):
            env[name] = value
    # Isaac's renderer stack wants a Vulkan ICD; same fallback as
    # scripts/play_isaac_velocity.sh. The nvidia ICD lives in
    # /usr/share/vulkan/icd.d on this machine (/etc/vulkan/icd.d is empty).
    nvidia_icd = None
    for candidate in (
        Path("/etc/vulkan/icd.d/nvidia_icd.json"),
        Path("/usr/share/vulkan/icd.d/nvidia_icd.json"),
    ):
        if candidate.is_file():
            nvidia_icd = candidate
            break
    if "VK_DRIVER_FILES" not in env and nvidia_icd is not None:
        env["VK_DRIVER_FILES"] = str(nvidia_icd)
        env["VK_ICD_FILENAMES"] = str(nvidia_icd)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT / "work/isaac_python_pkgs"), str(PROJECT_ROOT / "source/microduck_isaac_lab")]
    )
    env["MICRODUCK_PROJECT_DIR"] = str(PROJECT_ROOT)
    if extra:
        env.update(extra)
    return env


@dataclass
class LaunchTemplate:
    adapter: str
    argv: list[str]
    cwd: str
    env: dict[str, str]
    startup_marker: str | None
    startup_timeout_s: float
    wall_timeout_s: float
    sim_duration_s: float
    output_file: str | None
    trace_file: str | None
    expected: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        """Ledger/CLI-safe view: argv and cwd, never the environment."""
        return {
            "adapter": self.adapter,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "startup_marker": self.startup_marker,
            "startup_timeout_s": self.startup_timeout_s,
            "wall_timeout_s": self.wall_timeout_s,
            "sim_duration_s": self.sim_duration_s,
            "output_file": self.output_file,
            "trace_file": self.trace_file,
            "expected": dict(self.expected),
        }


def phase_steps(record: dict) -> int:
    """Phase-encoded duration is period_s x end_phase — never duration_s
    passed to --phase-clock."""
    command = record["command"]
    period_s = float(command["period_s"])
    end_phase = float(command["end_phase"])
    return round(period_s * end_phase * CONTROL_HZ)


class IsaacPlaybackAdapter:
    name = "isaac_playback"
    encodings = ("velocity", "phase", "posture_flag", "constant")

    def build(self, skill: str, record: dict, params: dict, work_dir: Path) -> LaunchTemplate:
        encoding = record.get("command", {}).get("encoding")
        if encoding not in self.encodings:
            raise ValueError(f"isaac_playback does not support encoding {encoding!r}")
        model = record["model"]["path"]
        run_dir = work_dir / "executions" / record["run_id"]
        output = run_dir / "playback.json"
        trace = run_dir / "trace.json"
        argv = [
            str(ISAACLAB_SH),
            "-p",
            str(PROJECT_ROOT / "scripts/play_isaac_velocity.py"),
            "--device",
            "cuda:0",
            "--policy-onnx",
            model,
            "--num-envs",
            "1",
            "--seed",
            "42",
            "--output",
            str(output),
            "--trace",
            str(trace),
            "--kit_args",
            ISAAC_KIT_ARGS,
        ]
        expected: dict = {
            "checkpoint": str(Path(model).resolve()),
            "encoding": encoding,
        }
        if encoding == "velocity":
            vx = float(params.get("vx", 0.0))
            vy = float(params.get("vy", 0.0))
            yaw = float(params.get("yaw", 0.0))
            argv += ["--command-vx", str(vx), "--command-vy", str(vy), "--command-yaw", str(yaw)]
            steps = int(record.get("defaults", {}).get("steps", 100))
            expected["command"] = [vx, vy, yaw]
        elif encoding == "phase":
            command = record["command"]
            period_s = float(command["period_s"])
            steps = phase_steps(record)
            argv += ["--phase-clock", repr(period_s)]
            expected["phase_clock_s"] = period_s
            expected["end_phase"] = float(command["end_phase"])
        elif encoding == "posture_flag":
            command = record["command"]
            flag = float(command["values"][record["posture"]])
            steps = int(record.get("defaults", {}).get("steps", 100))
            argv += ["--command-vx", str(flag), "--command-vy", "0.0", "--command-yaw", "0.0"]
            expected["command"] = [flag, 0.0, 0.0]
            expected["posture_flag"] = record["posture"]
        else:  # constant: command slots pinned to zero
            steps = int(round(float(record["timing"]["duration_s"]) * CONTROL_HZ))
            argv += ["--command-vx", "0.0", "--command-vy", "0.0", "--command-yaw", "0.0"]
            expected["command"] = [0.0, 0.0, 0.0]
        argv += ["--steps", str(steps)]
        expected["steps"] = steps
        return LaunchTemplate(
            adapter=self.name,
            argv=argv,
            cwd=str(PROJECT_ROOT),
            env=minimal_env(),
            startup_marker="MICRODUCK_PLAYBACK_CHECKPOINT=",
            startup_timeout_s=180.0,
            wall_timeout_s=600.0,
            sim_duration_s=steps * CONTROL_DT_S,
            output_file=str(output),
            trace_file=str(trace),
            expected=expected,
        )


class MujocoBaselineAdapter:
    name = "mujoco_baseline"
    encodings = ("velocity",)

    def build(self, skill: str, record: dict, params: dict, work_dir: Path) -> LaunchTemplate:
        encoding = record.get("command", {}).get("encoding")
        if encoding not in self.encodings:
            raise ValueError(
                f"mujoco_baseline does not support encoding {encoding!r} "
                "(velocity commands only)"
            )
        model = record["model"]["path"]
        run_dir = work_dir / "executions" / record["run_id"]
        output = run_dir / "mujoco_rollout.json"
        steps = int(record.get("defaults", {}).get("steps", 100))
        argv = [
            str(MUJOCO_PYTHON),
            str(PROJECT_ROOT / "scripts/run_mujoco_baseline.py"),
            "--policy",
            model,
            "--vx",
            str(float(params.get("vx", 0.0))),
            "--vy",
            str(float(params.get("vy", 0.0))),
            "--yaw-rate",
            str(float(params.get("yaw", 0.0))),
            "--duration",
            repr(steps * CONTROL_DT_S),
            "--output",
            str(output),
        ]
        return LaunchTemplate(
            adapter=self.name,
            argv=argv,
            cwd=str(PROJECT_ROOT),
            env=minimal_env(),
            startup_marker=None,
            startup_timeout_s=30.0,
            wall_timeout_s=120.0,
            sim_duration_s=steps * CONTROL_DT_S,
            output_file=str(output),
            trace_file=str(output),
            expected={"policy": str(Path(model).resolve()), "steps": steps, "encoding": encoding},
        )


ADAPTERS = {
    IsaacPlaybackAdapter.name: IsaacPlaybackAdapter(),
    MujocoBaselineAdapter.name: MujocoBaselineAdapter(),
}


def build_template(skill: str, record: dict, params: dict, work_dir: Path) -> LaunchTemplate:
    adapter_name = record.get("adapter", IsaacPlaybackAdapter.name)
    adapter = ADAPTERS.get(adapter_name)
    if adapter is None:
        raise ValueError(f"unknown adapter {adapter_name!r}")
    return adapter.build(skill, record, params, work_dir)
