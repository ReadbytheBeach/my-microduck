#!/usr/bin/env python3
"""Gateway: whitelist, per-encoding limits, usage/evidence state checks.

The gateway is the only authority on whether a proposal may proceed. It
never trusts model self-discipline, never infers compatibility from a
hash, and never lets limits leak across command encodings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .proposal import Proposal, ProposalError, validate_proposal


@dataclass
class GateResult:
    allowed: bool
    dry_run_only: bool = False
    reason: str = ""
    classification: str = ""  # empty when allowed; otherwise the error class
    labels: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "dry_run_only": self.dry_run_only,
            "reason": self.reason,
            "classification": self.classification,
            "labels": dict(self.labels),
        }


class Gateway:
    def __init__(
        self,
        registry: dict,
        experimental: bool = False,
        research: bool = False,
    ):
        self.registry = registry
        self.experimental = experimental
        self.research = research

    def skill_record(self, name: str) -> dict | None:
        return self.registry.get("skills", {}).get(name)

    def check(
        self,
        proposal: Proposal,
        execute: bool,
        now: float | None = None,
        max_steps: int = 1,
    ) -> GateResult:
        try:
            validate_proposal(proposal, now=now, max_steps=max_steps)
        except ProposalError as exc:
            return GateResult(False, reason=exc.reason, classification=exc.classification)

        step = proposal.steps[0]
        record = self.skill_record(step.skill)
        if record is None:
            return GateResult(
                False,
                reason=f"skill {step.skill!r} is not in the registry whitelist",
                classification="capability_gap",
            )

        if record.get("hash_status") == "mismatch":
            return GateResult(
                False,
                reason=f"model hash mismatch for {step.skill}; refusing to start",
                classification="policy_rejected",
            )

        limits_error = self._check_limits(record, step.params)
        if limits_error is not None:
            return GateResult(False, reason=limits_error, classification="policy_rejected")

        if step.skill == "stop":
            # Gateway built-in: no model, always permitted, handled locally.
            return GateResult(True, reason="stop is a gateway built-in")

        usage = record.get("usage_state", "registered")
        evidence = record.get("evidence_state", "unknown")
        labels: dict[str, Any] = {}

        if usage == "available":
            if evidence == "failed" and not self.research:
                return GateResult(
                    False,
                    reason=f"{step.skill} evidence is failed; needs explicit --research",
                    classification="policy_rejected",
                )
            if evidence == "failed":
                labels["research_replay"] = True
            return GateResult(True, dry_run_only=not execute, labels=labels)

        if usage == "experimental":
            if not self.experimental:
                return GateResult(
                    False,
                    reason=f"{step.skill} is experimental; pass --experimental to execute",
                    classification="policy_rejected",
                )
            if evidence in ("failed", "pending") and not self.research:
                return GateResult(
                    False,
                    reason=(
                        f"{step.skill} evidence is {evidence}; experimental execution "
                        "of a non-passed skill also needs explicit --research"
                    ),
                    classification="policy_rejected",
                )
            if evidence in ("failed", "pending"):
                labels["research_replay"] = True
            labels["experimental"] = True
            return GateResult(True, dry_run_only=not execute, labels=labels)

        # usage == "registered": display and dry-run only.
        if not execute:
            return GateResult(True, dry_run_only=True)
        if evidence in ("pending", "failed", "unknown") and self.research:
            # Research replay: explicitly requested by the operator, label
            # retained in ledger and result. This is also the bootstrap path
            # for M0-B verification replays of registered+unknown skills.
            labels["research_replay"] = True
            labels["evidence_state"] = evidence
            return GateResult(True, dry_run_only=False, labels=labels)
        return GateResult(
            False,
            reason=(
                f"{step.skill} is registered (evidence: {evidence}); normal mode "
                "allows dry-run only"
            ),
            classification="policy_rejected",
        )

    def _check_limits(self, record: dict, params: dict) -> str | None:
        """Limits are defined per command encoding and never shared."""
        encoding = record.get("command", {}).get("encoding")
        limits = record.get("limits", {})
        if encoding == "velocity":
            allowed = {"vx", "vy", "yaw"}
            unknown = set(params) - allowed
            if unknown:
                return f"velocity encoding takes only {sorted(allowed)}; got {sorted(unknown)}"
            bounds = limits.get("velocity", {})
            for key in params:
                low, high = bounds.get(key, (None, None))
                value = params[key]
                if low is None or not (low <= value <= high):
                    return f"{key}={value} outside velocity limit [{low}, {high}]"
            return None
        if encoding == "posture_flag":
            # Speed limits do not apply; only the contracted flag values do.
            if params:
                return "posture_flag encoding takes no velocity/params arguments"
            return None
        if encoding == "phase":
            if params:
                return (
                    "phase encoding takes no velocity params; duration comes from "
                    "period_s x end_phase"
                )
            return None
        if encoding == "constant":
            if params:
                return "constant encoding takes no command fields"
            return None
        if record.get("kind") == "builtin":
            return None if not params else "stop takes no params"
        return f"unknown command encoding {encoding!r}"
