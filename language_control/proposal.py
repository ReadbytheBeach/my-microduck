#!/usr/bin/env python3
"""Structured proposals for language control.

A proposal is the only thing the gateway evaluates. Proposals are produced
by the rule parser or (optionally) the Kimi client; both go through the
same strict validation here. M0 accepts exactly one step per proposal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
import uuid


PROPOSAL_TTL_S = 60.0

VALID_TARGETS = ("sim",)


class ProposalError(Exception):
    """Raised when a proposal fails structural validation.

    classification is one of the unified error classes:
    parse_error / provider_error / capability_gap / policy_rejected /
    execution_failed.
    """

    def __init__(self, classification: str, reason: str):
        super().__init__(f"{classification}: {reason}")
        self.classification = classification
        self.reason = reason


@dataclass
class Step:
    skill: str
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"skill": self.skill, "params": dict(self.params)}


@dataclass
class Proposal:
    proposal_id: str
    created_at: float
    target: str
    steps: list[Step]
    source: str  # "rule" | "llm"
    raw_text: str

    @property
    def expires_at(self) -> float:
        return self.created_at + PROPOSAL_TTL_S

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) > self.expires_at

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "target": self.target,
            "source": self.source,
            "raw_text": self.raw_text,
            "steps": [step.to_dict() for step in self.steps],
        }


def new_proposal(
    steps: list[Step],
    raw_text: str,
    source: str,
    target: str = "sim",
    created_at: float | None = None,
    max_steps: int = 1,
) -> Proposal:
    proposal = Proposal(
        proposal_id=uuid.uuid4().hex,
        created_at=time.time() if created_at is None else created_at,
        target=target,
        steps=steps,
        source=source,
        raw_text=raw_text,
    )
    validate_proposal(proposal, max_steps=max_steps)
    return proposal


def validate_proposal(
    proposal: Proposal, now: float | None = None, max_steps: int = 1
) -> None:
    """Structural rules: bounded step count, sim target, not expired.

    max_steps=1 keeps the M0 single-replay behaviour; the M1 session
    service passes a higher bound for sequences.
    """
    if proposal.target not in VALID_TARGETS:
        raise ProposalError(
            "policy_rejected",
            f"target {proposal.target!r} is not supported: M0 has no real-robot path",
        )
    if not 1 <= len(proposal.steps) <= max_steps:
        raise ProposalError(
            "policy_rejected",
            f"proposal has {len(proposal.steps)} steps, allowed 1..{max_steps}",
        )
    if proposal.expired(now):
        raise ProposalError(
            "policy_rejected",
            f"proposal expired (ttl {PROPOSAL_TTL_S:.0f}s)",
        )


_ALLOWED_LLM_KEYS = {"target", "steps"}
_ALLOWED_STEP_KEYS = {"skill", "params"}


def proposal_from_llm_payload(payload_text: str, raw_text: str) -> Proposal:
    """Strictly validate a Kimi JSON payload into a Proposal.

    Invalid JSON is parse_error; structurally valid but out-of-contract
    content is policy_rejected. The gateway does not rely on model
    self-discipline — this function and the gateway re-check everything.
    """
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ProposalError("parse_error", f"LLM output is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        raise ProposalError("parse_error", "LLM output must be a JSON object")
    extra = set(payload) - _ALLOWED_LLM_KEYS
    if extra:
        raise ProposalError("parse_error", f"LLM output has unexpected keys: {sorted(extra)}")
    target = payload.get("target", "sim")
    steps_raw = payload.get("steps")
    if not isinstance(steps_raw, list):
        raise ProposalError("parse_error", "LLM output must contain a steps list")
    if not steps_raw:
        raise ProposalError(
            "capability_gap", "model returned an empty steps list (no whitelisted skill)"
        )
    steps: list[Step] = []
    for index, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            raise ProposalError("parse_error", f"step {index} is not an object")
        extra_step = set(item) - _ALLOWED_STEP_KEYS
        if extra_step:
            raise ProposalError(
                "parse_error", f"step {index} has unexpected keys: {sorted(extra_step)}"
            )
        skill = item.get("skill")
        params = item.get("params", {})
        if not isinstance(skill, str) or not skill:
            raise ProposalError("parse_error", f"step {index} is missing a skill name")
        if not isinstance(params, dict):
            raise ProposalError("parse_error", f"step {index} params must be an object")
        steps.append(Step(skill=skill, params=params))
    # LLM proposals may be multi-step sequences (aligned with the session cap)
    return new_proposal(steps=steps, raw_text=raw_text, source="llm",
                        target=target, max_steps=8)
