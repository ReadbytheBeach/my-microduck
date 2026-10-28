#!/usr/bin/env python3
"""Build the language-control skill registry from three separate sources:

  upstream manifest/lock  (what the model is)
  + local skill config    (how it may be invoked)
  + acceptance records    (under which conditions it was verified)

The builder only ever *combines* these. Missing contract fields are marked
unverified; a file whose hash and shape check out is never promoted to
"compatible" or "accepted" on that basis; evidence_state comes only from
acceptance records (absent -> unknown, never passed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = PROJECT_ROOT / "upstream.lock"
DEFAULT_MANIFEST = PROJECT_ROOT / "reference/policies/v7/manifest.json"
DEFAULT_LOCAL = PROJECT_ROOT / "language_control/local_skills.json"
DEFAULT_ACCEPTANCE = PROJECT_ROOT / "language_control/acceptance_records.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "work/language_control/skill_registry.json"

VELOCITY_LIMITS = {"vx": [-0.4, 0.4], "vy": [-0.3, 0.3], "yaw": [-1.0, 1.0]}
VELOCITY_LIMITS_SOURCE = (
    "source/microduck_isaac_lab/microduck_isaac_lab/tasks/velocity/velocity_env_cfg.py:113-116"
)

OBS_SOURCE = (
    "reference/microduck_rl@ceca017 AGENTS.md invariants (61D = 48 base proprio "
    "+ 13 command [twist3, head_pose4, body_pose6]); cross-checked by "
    "scripts/run_mujoco_baseline.py obs assembly and velocity_env_cfg.py:147"
)
NORMALIZATION_SOURCE = (
    "reference/microduck_rl/scripts/export.py bakes the obs normalizer into the "
    "ONNX (AGENTS.md invariant); external normalization not required"
)
ACTION_SOURCE = (
    "14 servos in POLICY_JOINTS order, position offsets about HOME_POSE "
    "(scripts/run_mujoco_baseline.py:26-61, source/microduck_isaac_lab "
    "contract.py); Isaac play clips actions to [-1, 1]"
)
RUNTIME_ENV_SOURCE = (
    "isaac_sim 6.0.1 / isaac_lab 3.0.0-beta2 (upstream.lock validated_toolchain); "
    "robot USD assets/isaac/robot_allcollisions/robot_allcollisions.usda "
    "(upstream.lock bundled_isaac_asset d424a0c); adapter isaac_playback -> "
    "scripts/play_isaac_velocity.py via ISAACLAB_DIR/isaaclab.sh -p"
)


def field(value, source, verified=True):
    return {"value": value, "source": source, "status": "verified" if verified else "unverified"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_contract(skill: str, local: dict, manifest_entry: dict | None) -> dict:
    """Per-field contract record. kind (lifecycle) and command.encoding
    (input generation) stay independent dimensions."""
    contract: dict[str, dict] = {}
    if skill == "stop":
        return {"builtin": field("gateway built-in stop: cancels the active replay; no model", "language_control/gateway.py")}
    contract["obs"] = field(
        "61D: [base_ang_vel(3), projected_gravity(3), joint_pos(14), joint_vel(14), "
        "last_action(14), twist(3), head_pose(4), body_pose(6)]",
        OBS_SOURCE,
    )
    contract["normalization"] = field("baked into ONNX", NORMALIZATION_SOURCE)
    contract["action"] = field(
        "joint order / HOME_POSE offsets / clip [-1,1]", ACTION_SOURCE
    )
    contract["runtime_env"] = field("Isaac playback toolchain", RUNTIME_ENV_SOURCE)

    encoding = local["command"]["encoding"]
    if manifest_entry and "command" in manifest_entry:
        declared = manifest_entry["command"]
        if declared.get("encoding") != encoding:
            contract["command"] = field(
                f"local config says {encoding}, upstream manifest says "
                f"{declared.get('encoding')}: conflict",
                "reference/policies/v7/manifest.json",
                verified=False,
            )
        elif encoding == "phase":
            contract["command"] = field(
                f"phase: slots={declared['slots']}, period_s={declared['period_s']}, "
                f"end_phase={declared['end_phase']}; execution duration = period_s x "
                "end_phase (never pass duration_s to --phase-clock)",
                "reference/policies/v7/manifest.json",
            )
        elif encoding == "posture_flag":
            contract["command"] = field(
                f"posture_flag: slot={declared['slot']}, sit={declared['sit']}, "
                f"stand={declared['stand']}; speed limits not applicable",
                "reference/policies/v7/manifest.json",
            )
        else:
            contract["command"] = field(
                f"{encoding} (upstream declared)", "reference/policies/v7/manifest.json"
            )
    elif encoding == "phase":
        command = local["command"]
        contract["command"] = field(
            f"phase (locally declared): period_s={command['period_s']}, "
            f"end_phase={command['end_phase']}",
            "language_control/local_skills.json + work/video_dance/docs/"
            "视频驱动MicroDuck舞蹈全流程操作说明_不同摩擦和硬度.md:553",
            verified=False,
        )
    else:
        contract["command"] = field(
            f"{encoding} (locally declared; upstream manifest has no command block)",
            "language_control/local_skills.json",
            verified=False,
        )

    if encoding == "velocity" and skill == "walk":
        description = (manifest_entry or {}).get("description", "")
        task_id = (manifest_entry or {}).get("training", {}).get("task_id", "")
        if "walk" in description.lower() or "VelStand" in task_id:
            contract["command"] = field(
                "velocity: twist slot [vx, vy, yaw]; velstand is the v7 default walk "
                "policy (manifest slot=walk, training task Mjlab-VelStand-*)",
                "reference/policies/v7/manifest.json (slot/description/training)",
            )

    kind = local["kind"]
    timing_bits = [f"control 50 Hz (manifest robot.control_hz)"]
    timing_ok = True
    if kind == "episodic" and manifest_entry:
        duration = manifest_entry.get("duration_s")
        timing_bits.append(f"episodic duration_s={duration}")
        if encoding == "phase" and "command" in manifest_entry:
            declared = manifest_entry["command"]
            computed = declared["period_s"] * declared["end_phase"]
            if abs(computed - duration) > 1e-6:
                timing_bits.append(
                    f"INCONSISTENT: period_s x end_phase = {computed} != duration_s = {duration}"
                )
                timing_ok = False
            else:
                timing_bits.append("duration_s == period_s x end_phase (consistent)")
    elif kind == "scripted" and manifest_entry:
        timing_bits.append(
            f"scripted ramp_s={manifest_entry.get('ramp_s')}, "
            f"unwind_s={manifest_entry.get('unwind_s')} (upstream declared)"
        )
        timing_ok = False  # fixed-flag reproduction of ramp/unwind not yet verified
        contract["timing_note"] = field(
            "whether a fixed posture flag reproduces the ramp/unwind profile is "
            "NOT verified; sitstand stays registered+pending",
            "plan red line; upstream.lock [playground_sitstand] note",
            verified=False,
        )
    elif kind == "perpetual":
        timing_bits.append("perpetual: no fixed end; M0 replay length = adapter steps")
    contract["timing"] = field("; ".join(timing_bits), "reference/policies/v7/manifest.json / language_control/local_skills.json", verified=timing_ok)

    if skill == "crouch":
        contract["action"] = field(
            "manifest declares mode=roller, action_scale=0.8, but the local Isaac "
            "playback interface (scripts/play_isaac_velocity.py) has no action-scale "
            "or roller-model argument: skill registered, not enabled",
            "reference/policies/v7/manifest.json vs scripts/play_isaac_velocity.py CLI",
            verified=False,
        )
    return contract


def contract_status(contract: dict) -> str:
    statuses = [entry["status"] for entry in contract.values()]
    if all(status == "verified" for status in statuses):
        return "verified"
    if any(status == "verified" for status in statuses):
        return "partial"
    return "unverified"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--local", type=Path, default=DEFAULT_LOCAL)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    lock_hashes = lock["policies"]["files"]
    policies_pin = lock["policies"]
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_by_file = {entry["file"]: entry for entry in manifest["policies"]}
    local_doc = json.loads(args.local.read_text(encoding="utf-8"))
    local = local_doc["skills"]
    acceptance = json.loads(args.acceptance.read_text(encoding="utf-8"))["records"]

    # newest record per skill wins; no record -> unknown (never passed)
    evidence: dict[str, dict] = {}
    for record in sorted(acceptance, key=lambda r: r["date"]):
        evidence[record["skill"]] = record

    warnings: list[str] = []
    skills: dict[str, dict] = {}
    for name, cfg in local.items():
        record: dict = {
            "kind": cfg["kind"],
            "adapter": cfg.get("adapter"),
            "defaults": cfg.get("defaults", {}),
        }
        if "scope_note" in cfg:
            record["scope_note"] = cfg["scope_note"]
        if cfg["kind"] == "builtin":
            record.update(
                usage_state=cfg["usage_state"],
                evidence_state=evidence.get(name, {}).get("state", "unknown"),
                contract={"builtin": field("gateway built-in", "language_control/gateway.py")},
                contract_status="verified",
                hash_status="no_model",
            )
            skills[name] = record
            continue

        model_path = PROJECT_ROOT / cfg["model"]["path"]
        entry: dict = {"model": {"path": str(model_path), "exists": model_path.is_file()}}
        lock_key = cfg["model"].get("lock_key")
        if lock_key is not None:
            expected = lock_hashes.get(lock_key)
            entry["model"]["expected_sha256"] = expected
            entry["model"]["sha256_source"] = f"upstream.lock policies.files[{lock_key}] v7@{policies_pin['revision'][:7]}"
        else:
            expected = cfg["model"].get("sha256")
            entry["model"]["expected_sha256"] = expected
            entry["model"]["sha256_source"] = cfg["model"].get("provenance", "local config")
        if model_path.is_file():
            actual = file_sha256(model_path)
            entry["model"]["actual_sha256"] = actual
            entry["hash_status"] = "ok" if actual == expected else "mismatch"
            if actual != expected:
                warnings.append(f"{name}: hash mismatch {actual[:8]} != {str(expected)[:8]}")
        else:
            entry["hash_status"] = "missing"
            warnings.append(f"{name}: model file missing: {model_path}")

        manifest_entry = manifest_by_file.get(lock_key) if lock_key else None
        entry["manifest_entry"] = manifest_entry
        contract = build_contract(name, cfg, manifest_entry)
        entry["contract"] = contract
        entry["contract_status"] = contract_status(contract)

        command = dict(cfg["command"])
        if manifest_entry and "command" in manifest_entry:
            declared = manifest_entry["command"]
            if declared.get("encoding") == command.get("encoding") == "phase":
                command["period_s"] = declared["period_s"]
                command["end_phase"] = declared["end_phase"]
                command["slots"] = declared["slots"]
            elif declared.get("encoding") == command.get("encoding") == "posture_flag":
                command["values"] = {"sit": declared["sit"], "stand": declared["stand"]}
                command["slot"] = declared["slot"]
        entry["command"] = command
        if cfg["command"]["encoding"] == "velocity":
            entry["limits"] = {"velocity": VELOCITY_LIMITS, "source": VELOCITY_LIMITS_SOURCE}
        if "posture" in cfg:
            entry["posture"] = cfg["posture"]
        if manifest_entry and manifest_entry.get("duration_s") is not None:
            entry["timing"] = {"duration_s": manifest_entry["duration_s"]}

        ev = evidence.get(name)
        entry["evidence_state"] = ev["state"] if ev else "unknown"
        if ev:
            entry["evidence"] = {
                "state": ev["state"],
                "date": ev["date"],
                "source": ev["source"],
                "scope": ev.get("scope", {}),
                "summary": ev["summary"],
            }

        usage = cfg["usage_state"]
        if usage == "available":
            problems = []
            if entry["evidence_state"] != "passed":
                problems.append(f"evidence is {entry['evidence_state']}, not passed")
            if entry["contract_status"] != "verified":
                problems.append(f"contract is {entry['contract_status']}, not verified")
            if entry["hash_status"] != "ok":
                problems.append(f"hash status is {entry['hash_status']}")
            if problems:
                warnings.append(
                    f"{name}: requested usage_state=available demoted to registered: "
                    + "; ".join(problems)
                )
                usage = "registered"
        entry["usage_state"] = usage
        record.update(entry)
        skills[name] = record

    # Unbound models stay visible: file count, skill count, enabled count
    # are three separate numbers.
    bound_files = {
        Path(record["model"]["path"]).name
        for record in skills.values()
        if record.get("model")
    }
    unbound_v7 = sorted(set(lock_hashes) - bound_files - {"manifest.json", "README.md"})
    legacy = lock.get("playground_sitstand", {})

    # M1-C: directed transition edges; cross-model edges stay "unverified"
    # until a real switch with recorded A/B hashes and switch step exists.
    transition_edges = []
    for edge in local_doc.get("transition_edges", []):
        enriched = dict(edge)
        for key in ("source", "target"):
            record = skills.get(edge.get(key, ""), {})
            model = record.get("model") or {}
            enriched[f"{key}_model_sha256"] = model.get("actual_sha256")
        transition_edges.append(enriched)

    registry = {
        "schema": 1,
        "generated_from": {
            "upstream_lock": str(args.lock),
            "manifest": str(args.manifest),
            "local_config": str(args.local),
            "acceptance_records": str(args.acceptance),
        },
        "counts": {
            "model_files": len(bound_files) + len(unbound_v7) + (1 if legacy else 0),
            "skills": len(skills),
            "skills_available": sum(1 for r in skills.values() if r["usage_state"] == "available"),
            "skills_experimental": sum(1 for r in skills.values() if r["usage_state"] == "experimental"),
            "skills_registered": sum(1 for r in skills.values() if r["usage_state"] == "registered"),
        },
        "unbound_models": {
            "v7": unbound_v7,
            "legacy_sitstand": {
                "path": "reference/microduck/policies/alpha_sitstand.onnx",
                "revision": legacy.get("revision"),
                "sha256": legacy.get("sha256"),
                "note": "legacy playground sitstand registered separately; "
                "same-name silent switching with v7 alpha_sitstand is forbidden",
            },
        },
        "skills": skills,
        "transition_edges": transition_edges,
        "warnings": warnings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"registry written: {args.output}")
    print(json.dumps(registry["counts"], indent=2))
    for warning in warnings:
        print(f"WARNING {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
