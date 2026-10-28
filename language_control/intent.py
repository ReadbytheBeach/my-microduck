#!/usr/bin/env python3
"""Intent parsing: a finite full-match rule grammar plus an optional Kimi client.

The rule parser is the default path. It is a *complete-match* finite
grammar, not keyword extraction: anything that does not match a whitelist
phrase exactly is refused (parse_error), routed to a capability gap, or
rejected as policy. Negation, conditionals, distance/duration goals and
multi-step requests never produce a proposal.

The Kimi client speaks to an OpenAI-compatible chat endpoint over urllib
and asks for strict JSON Schema output, falling back to plain JSON mode
when the endpoint rejects response_format (the gateway validates output
independently either way). It is only used when explicitly enabled with
--llm and a KIMI_LC_API_KEY; secrets never enter the ledger.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

from .proposal import ProposalError, Step, new_proposal, proposal_from_llm_payload


DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k3"
REQUEST_TIMEOUT_S = 20.0

# Full-match whitelist: exact phrase -> (skill, params). Params are the
# canonical defaults for M0 single replay; they are part of the controlled
# local configuration, not model output.
WHITELIST: dict[str, tuple[str, dict]] = {
    # walk (velocity encoding)
    "向前走": ("walk", {"vx": 0.15}),
    "往前走": ("walk", {"vx": 0.15}),
    "前进": ("walk", {"vx": 0.15}),
    "走": ("walk", {"vx": 0.15}),
    "向后走": ("walk", {"vx": -0.15}),
    "后退": ("walk", {"vx": -0.15}),
    "向左走": ("walk", {"vy": 0.15}),
    "向右走": ("walk", {"vy": -0.15}),
    "左转": ("walk", {"yaw": 0.5}),
    "右转": ("walk", {"yaw": -0.5}),
    "原地走": ("walk", {}),
    # stand (velocity encoding, zero command)
    "站立": ("stand", {}),
    "站好": ("stand", {}),
    "站着": ("stand", {}),
    "站住": ("stand", {}),
    "别动": ("stand", {}),
    # sitstand (posture_flag encoding)
    "坐下": ("sitdown", {}),
    "坐": ("sitdown", {}),
    "站起来": ("standup", {}),
    "起立": ("standup", {}),
    "起身": ("standup", {}),
    # roulade (constant encoding)
    "翻跟头": ("roulade", {}),
    "翻个跟头": ("roulade", {}),
    "翻滚": ("roulade", {}),
    # kicks (constant encoding)
    "左踢": ("kick_left", {}),
    "踢左边": ("kick_left", {}),
    "左脚踢": ("kick_left", {}),
    "右踢": ("kick_right", {}),
    "踢右边": ("kick_right", {}),
    "右脚踢": ("kick_right", {}),
    # crouch (phase encoding)
    "蹲下": ("crouch", {}),
    "蹲": ("crouch", {}),
    # ground_pick (phase encoding)
    "捡东西": ("ground_pick", {}),
    "捡起来": ("ground_pick", {}),
    "拾取": ("ground_pick", {}),
    # dance_side_step (phase encoding, experimental+failed: explicit name only)
    "跳侧步舞": ("dance_side_step", {}),
    "侧步舞": ("dance_side_step", {}),
    "侧步跳舞": ("dance_side_step", {}),
    # stop (gateway built-in, no model; normally intercepted before parsing)
    "停止": ("stop", {}),
    "停下": ("stop", {}),
    "停": ("stop", {}),
}

STOP_PHRASES = frozenset(
    phrase for phrase, (skill, _params) in WHITELIST.items() if skill == "stop"
)

_DISTANCE_RE = re.compile(r"(米|厘米|毫米|公里|分钟|秒钟|秒|小时)")
_CONDITIONAL_RE = re.compile(r"(如果|要是|若|当.+时|等到|一旦)")
_SEQUENCE_RE = re.compile(r"(然后|接着|之后|随后|先.+再|再.+然后)")
_NEGATION_RE = re.compile(r"(不要|不准|禁止|千万别|不能)")
# Generic dance requests: the alias must NOT auto-bind to dance_side_step.
_GENERIC_DANCE_RE = re.compile(r"^(跳个舞|跳支舞|跳舞|来段舞|表演舞蹈)$")


class IntentParser:
    """Finite full-match grammar for M0."""

    def parse(self, text: str) -> "object":
        normalized = "".join(text.split())
        if not normalized:
            raise ProposalError("parse_error", "empty input")
        if normalized in WHITELIST:
            skill, params = WHITELIST[normalized]
            return new_proposal(
                steps=[Step(skill=skill, params=dict(params))],
                raw_text=text,
                source="rule",
            )
        if _CONDITIONAL_RE.search(normalized):
            raise ProposalError(
                "parse_error",
                "conditional expressions are outside the M0 finite grammar; refused",
            )
        if _NEGATION_RE.search(normalized):
            raise ProposalError(
                "parse_error",
                "negated expressions never map to an action; nothing executed",
            )
        if _SEQUENCE_RE.search(normalized):
            raise ProposalError(
                "policy_rejected",
                "multi-step instructions are rejected as a whole in M0 (sequencing is M1)",
            )
        if _DISTANCE_RE.search(normalized):
            raise ProposalError(
                "capability_gap",
                "distance/duration goals are not supported in M0 and are never "
                "rewritten into velocity/duration commands (M1)",
            )
        if _GENERIC_DANCE_RE.match(normalized):
            raise ProposalError(
                "capability_gap",
                "generic dance requests do not auto-bind; name the skill "
                "explicitly (dance_side_step / 跳侧步舞)",
            )
        raise ProposalError(
            "capability_gap",
            f"no whitelisted skill matches {text!r} (unknown skill or phrasing)",
        )


# ---------------------------------------------------------------------------
# M1-B: bounded sequence grammar (same-policy velocity sessions)
# ---------------------------------------------------------------------------

MAX_SEQUENCE_STEPS = 8

# connectives split segments; punctuation is dropped first
_SEQUENCE_SPLIT_RE = re.compile(r"(?:然后|接着|之后|随后|再)")
# trailing duration, e.g. 走两秒 / 走2秒 / 走2.5秒 / 站十秒钟
_DURATION_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])(?:秒钟|秒)$")
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
# distance units are still a capability gap; 秒/分钟 are durations handled above
_DISTANCE_ONLY_RE = re.compile(r"(米|厘米|毫米|公里|分钟|小时)")
# skills that accept an explicit sim-duration parameter in M1 sequences
_DURATION_SKILLS = ("walk", "stand")
DEFAULT_STEP_DURATION_S = 2.0


def _parse_duration(token: str) -> float:
    if token in _CN_DIGITS:
        return float(_CN_DIGITS[token])
    return float(token)


def _cn_number(token: str) -> float:
    """中文数字: 单字(三)、十组合(三十/十五/四十五)、百组合(三百/一百二);否则按浮点."""
    if token in _CN_DIGITS:
        return float(_CN_DIGITS[token])
    total = 0
    rest = token
    if "百" in rest:
        left, _, rest = rest.partition("百")
        total += (_CN_DIGITS.get(left, 1) if left else 1) * 100
    if "十" in rest:
        left, _, right = rest.partition("十")
        total += (_CN_DIGITS.get(left, 1) if left else 1) * 10
        total += _CN_DIGITS.get(right, 0) if right else 0
    elif rest:
        total += _CN_DIGITS[rest] if rest in _CN_DIGITS else float(rest)
    if total:
        return float(total)
    return float(token)


# §3.6 closed-loop goals: 走30厘米 / 向前走0.3米 / 左转30度
_DISTANCE_GOAL_RE = re.compile(
    r"^(向前走|往前走|前进|走)?(\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)(毫米|厘米|米)$")
_TURN_GOAL_RE = re.compile(
    r"^(左转|右转|向左转|向右转)(\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)度$")
_UNIT_TO_M = {"毫米": 0.001, "厘米": 0.01, "米": 1.0}


class SequenceParser(IntentParser):
    """M1-B rule grammar: comma/connective-separated whitelist phrases with
    optional 秒-duration suffixes. Everything that is not a full whitelist
    match per segment is refused with the same error classes as M0. No LLM,
    no keyword guessing; negation/conditionals/distance stay rejected.
    """

    def parse(self, text: str) -> "object":
        normalized = "".join(text.split())
        if not normalized:
            raise ProposalError("parse_error", "empty input")
        if _CONDITIONAL_RE.search(normalized):
            raise ProposalError(
                "parse_error",
                "conditional expressions are outside the finite grammar; refused",
            )
        if _NEGATION_RE.search(normalized):
            raise ProposalError(
                "parse_error",
                "negated expressions never map to an action; nothing executed",
            )
        if _GENERIC_DANCE_RE.match(normalized):
            raise ProposalError(
                "capability_gap",
                "generic dance requests do not auto-bind; name the skill "
                "explicitly (dance_side_step / 跳侧步舞)",
            )
        segments = [
            segment.strip("，,;；。.、")
            for segment in _SEQUENCE_SPLIT_RE.split(normalized)
        ]
        segments = [segment for segment in segments if segment]
        if len(segments) > MAX_SEQUENCE_STEPS:
            raise ProposalError(
                "policy_rejected",
                f"sequences are limited to {MAX_SEQUENCE_STEPS} steps "
                f"(got {len(segments)})",
            )
        steps: list[Step] = []
        for segment in segments:
            steps.append(self._parse_segment(segment, text))
        return new_proposal(
            steps=steps, raw_text=text, source="rule", max_steps=MAX_SEQUENCE_STEPS
        )

    def _parse_segment(self, segment: str, raw_text: str) -> Step:
        duration_s: float | None = None
        match = _DURATION_SUFFIX_RE.search(segment)
        if match:
            duration_s = _parse_duration(match.group(1))
            if duration_s <= 0:
                raise ProposalError("parse_error", "duration must be positive")
            segment = segment[: match.start()]
        # closed-loop distance/turn goals (sim_ground_truth feedback)
        distance_match = _DISTANCE_GOAL_RE.match(segment)
        if distance_match:
            distance_m = _cn_number(distance_match.group(2)) * _UNIT_TO_M[distance_match.group(3)]
            return Step(skill="walk", params={"distance_m": distance_m})
        turn_match = _TURN_GOAL_RE.match(segment)
        if turn_match:
            sign = -1.0 if turn_match.group(1) in ("右转", "向右转") else 1.0
            return Step(skill="walk", params={"turn_deg": sign * _cn_number(turn_match.group(2))})
        if _DISTANCE_ONLY_RE.search(segment):
            raise ProposalError(
                "capability_gap",
                "distance goals are not rewritten into velocity/duration commands",
            )
        if segment not in WHITELIST:
            raise ProposalError(
                "capability_gap",
                f"no whitelisted skill matches segment {segment!r} of {raw_text!r}",
            )
        skill, params = WHITELIST[segment]
        params = dict(params)
        if duration_s is not None:
            if skill not in _DURATION_SKILLS:
                raise ProposalError(
                    "capability_gap",
                    f"explicit duration is only supported for {_DURATION_SKILLS}; "
                    f"{skill} duration is fixed by its contract",
                )
            params["sim_duration_s"] = duration_s
        return Step(skill=skill, params=params)


PROPOSAL_JSON_SCHEMA = {
    "name": "microduck_proposal",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": ["sim"]},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "params": {"type": "object"},
                    },
                    "required": ["skill", "params"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["target", "steps"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = (
    "You map a Chinese instruction to exactly one MicroDuck skill from this "
    "whitelist: walk, stand, sitdown, standup, roulade, kick_left, kick_right, "
    "crouch, ground_pick, dance_side_step, stop. If the instruction is "
    "negated, conditional, multi-step, distance/duration-based, or names no "
    "whitelisted skill, refuse by returning an empty steps array. Never "
    "invent paths, task names, or shell fragments."
)


class KimiClient:
    """OpenAI-compatible chat endpoint over urllib; strict schema with fallback."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
    ):
        self.base_url = (base_url or os.environ.get("KIMI_LC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("KIMI_LC_API_KEY") or os.environ.get("MOONSHOT_API_KEY", "")
        )
        self.model = model or os.environ.get("KIMI_LC_MODEL") or DEFAULT_MODEL
        self.temperature = float(os.environ.get("KIMI_LC_TEMPERATURE", "0"))
        self.timeout_s = timeout_s
        self.strict_schema_supported: bool | None = None

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise ProposalError(
                "provider_error", f"endpoint HTTP {exc.code}: {body or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProposalError("provider_error", f"endpoint unreachable: {exc}") from exc

    def _request_payload(self, text: str, strict: bool) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
        }
        if strict:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": PROPOSAL_JSON_SCHEMA,
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def complete(self, text: str) -> str:
        """Return the raw content string; raises ProposalError(provider_error)."""
        if not self.api_key:
            raise ProposalError(
                "provider_error", "KIMI_LC_API_KEY is not set; LLM parsing disabled"
            )
        strict = self.strict_schema_supported is not False
        try:
            response = self._post(self._request_payload(text, strict=strict))
            if strict:
                self.strict_schema_supported = True
        except ProposalError as exc:
            if exc.classification == "provider_error" and "HTTP 400" in exc.reason:
                if "temperature" in exc.reason:
                    # Some models (e.g. kimi-k3) accept only temperature=1.
                    self.temperature = 1.0
                    return self.complete(text)
                if strict:
                    # Endpoint rejected strict JSON Schema: fall back to plain
                    # JSON mode. The gateway validates output independently.
                    self.strict_schema_supported = False
                    response = self._post(self._request_payload(text, strict=False))
                else:
                    raise
            else:
                raise
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProposalError(
                "provider_error", f"malformed endpoint response: {exc}"
            ) from exc

    def parse(self, text: str):
        content = self.complete(text)
        return proposal_from_llm_payload(content, raw_text=text)

    def probe(self, probe_file: Path | None = None) -> dict:
        """Minimal live probe: does the endpoint honour strict JSON Schema?"""
        content = self.complete("站立")
        result = {
            "endpoint": self.base_url,
            "model": self.model,
            "strict_schema_supported": self.strict_schema_supported,
            "sample_content": content[:200],
        }
        if probe_file is not None:
            probe_file.parent.mkdir(parents=True, exist_ok=True)
            probe_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result
