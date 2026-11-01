#!/usr/bin/env python3
"""M0-A offline acceptance for language control.

Covers the review-mandated minimal set: hash-mismatch refusal, shape-ok
but unverified encoding not auto-enabled, per-encoding limits that never
leak across encodings, semantic counterexamples (negation / conditional /
distance / multi-step), pending/failed gating with explicit research
mode, cancel during starting and running with process-group cleanup,
exit-code-0-with-missing-result is not success, distinct error classes,
real-target refusal, duplicate submission, registry three-way
consistency, and ledger chain integrity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from language_control import adapters  # noqa: E402
from language_control.adapters import LaunchTemplate, build_template, phase_steps  # noqa: E402
from language_control.cli import LanguageControlApp  # noqa: E402
from language_control.executor import Executor  # noqa: E402
from language_control.gateway import Gateway  # noqa: E402
from language_control.intent import IntentParser, KimiClient  # noqa: E402
from language_control.ledger_log import Ledger  # noqa: E402
from language_control.proposal import (  # noqa: E402
    ProposalError,
    Step,
    new_proposal,
    proposal_from_llm_payload,
    validate_proposal,
)

import build_skill_registry  # noqa: E402
import foot_structure_ledger  # noqa: E402

REGISTRY_PATH = ROOT / "work/language_control/skill_registry.json"


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def make_proposal(skill: str, params: dict | None = None, **kwargs) -> object:
    return new_proposal(steps=[Step(skill=skill, params=params or {})], raw_text=skill, source="rule", **kwargs)


def stub_template(argv, marker=None, output=None, trace=None, expected=None,
                  startup_timeout=5.0, wall_timeout=20.0) -> LaunchTemplate:
    return LaunchTemplate(
        adapter="stub",
        argv=argv,
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        startup_marker=marker,
        startup_timeout_s=startup_timeout,
        wall_timeout_s=wall_timeout,
        sim_duration_s=1.0,
        output_file=output,
        trace_file=trace,
        expected=expected or {},
    )


def wait_state(executor: Executor, states: tuple[str, ...], timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if executor.state in states:
            return executor.state
        time.sleep(0.02)
    raise AssertionError(f"executor stuck at {executor.state}, wanted {states}")


class RegistryConsistencyTests(unittest.TestCase):
    """三方一致性: upstream lock/manifest + local config + acceptance records."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "registry.json"
            argv = sys.argv
            sys.argv = ["build_skill_registry.py", "--output", str(out)]
            try:
                rc = build_skill_registry.main()
            finally:
                sys.argv = argv
            assert rc == 0
            cls.registry = json.loads(out.read_text(encoding="utf-8"))

    def test_eleven_skills_no_padding(self):
        skills = self.registry["skills"]
        # 2026-11-01: +walk_backward（walk 的后退变体独立成技,available+passed）
        self.assertEqual(
            sorted(skills),
            sorted(["walk", "walk_backward", "stand", "sitdown", "standup",
                    "roulade", "kick_left", "kick_right", "crouch", "ground_pick",
                    "dance_side_step", "stop"]),
        )
        counts = self.registry["counts"]
        self.assertEqual(counts["skills"], 12)
        # model file count, skill count, enabled count are separate numbers
        self.assertEqual(counts["model_files"], 12)  # 10 v7 + dance V4 + legacy sitstand
        self.assertLessEqual(counts["skills_available"], counts["skills"])

    def test_all_model_hashes_match(self):
        for name, record in self.registry["skills"].items():
            if record.get("model"):
                self.assertEqual(record["hash_status"], "ok", name)

    def test_usage_and_evidence_states(self):
        skills = self.registry["skills"]
        self.assertEqual(skills["dance_side_step"]["usage_state"], "experimental")
        self.assertEqual(skills["dance_side_step"]["evidence_state"], "failed")
        self.assertEqual(skills["sitdown"]["evidence_state"], "pending")
        self.assertEqual(skills["sitdown"]["usage_state"], "registered")
        self.assertEqual(skills["standup"]["evidence_state"], "pending")
        # M0-B verified skills promoted to available with passed evidence
        # (walk_backward: 2026-11-01 vx=-0.15 真实核验通过后晋升)
        for name in ("walk", "walk_backward", "ground_pick"):
            self.assertEqual(skills[name]["usage_state"], "available", name)
            self.assertEqual(skills[name]["evidence_state"], "passed", name)
            self.assertEqual(skills[name]["contract_status"], "verified", name)
        for name in ("stand", "crouch"):
            self.assertEqual(skills[name]["usage_state"], "registered", name)
        # 2026-11-01: stand 零速切换真实核验通过(velstand,scope 限定);
        # alpha_stand.onnx 本体未验,usage 保持 registered
        self.assertEqual(skills["stand"]["evidence_state"], "passed")
        self.assertEqual(skills["crouch"]["evidence_state"], "unknown")
        # M2 核验回放后: roulade 有真实旋转但 done_count=4 -> pending;
        # kick 指定腿轨迹通过(sim_smoke_passed)但契约 partial -> 保持 registered
        self.assertEqual(skills["roulade"]["evidence_state"], "pending")
        self.assertEqual(skills["roulade"]["usage_state"], "registered")
        for name in ("kick_left", "kick_right"):
            self.assertEqual(skills[name]["evidence_state"], "passed", name)
            self.assertEqual(skills[name]["usage_state"], "registered", name)
        # evidence only ever comes from acceptance records
        for name, record in skills.items():
            if record["evidence_state"] == "passed":
                self.assertIn("evidence", record)
        # available implies passed evidence and verified contract
        for name, record in skills.items():
            if record["usage_state"] == "available" and record["kind"] != "builtin":
                self.assertEqual(record["evidence_state"], "passed", name)
                self.assertEqual(record["contract_status"], "verified", name)

    def test_unbound_and_legacy_models_visible(self):
        unbound = self.registry["unbound_models"]
        self.assertIn("alpha_walking.onnx", unbound["v7"])
        self.assertIn("roller.onnx", unbound["v7"])
        legacy = unbound["legacy_sitstand"]
        self.assertEqual(legacy["sha256"], "c6c40e35e726eabd803d633e090d112994f469921152448367953fbaf9799bc8")
        # legacy and v7 sitstand are distinct registrations, no silent swap
        v7_sitstand = self.registry["skills"]["sitdown"]["model"]["expected_sha256"]
        self.assertNotEqual(legacy["sha256"], v7_sitstand)

    def test_hash_mismatch_blocks_start(self):
        registry = load_registry()
        tampered = json.loads(json.dumps(registry))
        tampered["skills"]["walk"]["hash_status"] = "mismatch"
        gateway = Gateway(tampered)
        result = gateway.check(make_proposal("walk", {"vx": 0.1}), execute=True)
        self.assertFalse(result.allowed)
        self.assertEqual(result.classification, "policy_rejected")
        self.assertIn("hash", result.reason)

    def test_builder_flags_tampered_hash(self):
        local = json.loads((ROOT / "language_control/local_skills.json").read_text(encoding="utf-8"))
        local["skills"]["dance_side_step"]["model"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "local.json"
            cfg.write_text(json.dumps(local), encoding="utf-8")
            out = Path(tmp) / "registry.json"
            argv = sys.argv
            sys.argv = ["build_skill_registry.py", "--local", str(cfg), "--output", str(out)]
            try:
                build_skill_registry.main()
            finally:
                sys.argv = argv
            registry = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(registry["skills"]["dance_side_step"]["hash_status"], "mismatch")
        self.assertTrue(any("dance_side_step" in w for w in registry["warnings"]))

    def test_shape_ok_unverified_encoding_not_auto_enabled(self):
        """61D obs / 14D action + hash ok must NOT promote a skill whose
        command encoding is unverified. The builder demotes it."""
        local = json.loads((ROOT / "language_control/local_skills.json").read_text(encoding="utf-8"))
        local["skills"]["stand"]["usage_state"] = "available"  # try to sneak in
        acceptance = json.loads((ROOT / "language_control/acceptance_records.json").read_text(encoding="utf-8"))
        acceptance["records"].append({
            "skill": "stand", "state": "passed", "date": "2026-10-26",
            "source": "test", "summary": "fake pass",
        })
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "local.json"
            acc = Path(tmp) / "acc.json"
            out = Path(tmp) / "registry.json"
            cfg.write_text(json.dumps(local), encoding="utf-8")
            acc.write_text(json.dumps(acceptance), encoding="utf-8")
            argv = sys.argv
            sys.argv = [
                "build_skill_registry.py", "--local", str(cfg),
                "--acceptance", str(acc), "--output", str(out),
            ]
            try:
                build_skill_registry.main()
            finally:
                sys.argv = argv
            registry = json.loads(out.read_text(encoding="utf-8"))
        stand = registry["skills"]["stand"]
        # hash ok and 61/14 shape documented, but command encoding is only
        # locally declared (contract partial) -> must not become available
        self.assertEqual(stand["hash_status"], "ok")
        self.assertEqual(stand["contract_status"], "partial")
        self.assertEqual(stand["usage_state"], "registered")
        self.assertTrue(any("stand" in w for w in registry["warnings"]))


class RuleParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser()

    def test_whitelist_full_match(self):
        proposal = self.parser.parse("向前走")
        self.assertEqual(proposal.steps[0].skill, "walk")
        self.assertEqual(proposal.steps[0].params, {"vx": 0.15})
        self.assertEqual(self.parser.parse("蹲下").steps[0].skill, "crouch")
        self.assertEqual(self.parser.parse("站起来").steps[0].skill, "standup")

    def test_colloquial_aliases(self):
        """2026-11-01 语音场景扩充的口语同义词,映射到同一受控技能."""
        cases = {
            "走路": ("walk", {"vx": 0.15}),
            "朝前走": ("walk", {"vx": 0.15}),
            "往后走": ("walk_backward", {"vx": -0.15}),
            "退后": ("walk_backward", {"vx": -0.15}),
            "往左走": ("walk", {"vy": 0.15}),
            "向右转": ("walk", {"yaw": -0.5}),
            "站定": ("stand", {}),
            "站着别动": ("stand", {}),
            "坐下去": ("sitdown", {}),
            "起来": ("standup", {}),
            "打个滚": ("roulade", {}),
            "踢左腿": ("kick_left", {}),
            "踢右腿": ("kick_right", {}),
            "下蹲": ("crouch", {}),
            "把东西捡起来": ("ground_pick", {}),
            "停住": ("stop", {}),
            "别走了": ("stop", {}),
        }
        for text, (skill, params) in cases.items():
            with self.subTest(text=text):
                step = self.parser.parse(text).steps[0]
                self.assertEqual((step.skill, step.params), (skill, params))

    def test_walk_backward_duration(self):
        """walk_backward 与 walk 一样接受显式时长（序列解析器）."""
        from language_control.intent import SequenceParser
        step = SequenceParser().parse("向后走两秒").steps[0]
        self.assertEqual(
            (step.skill, step.params),
            ("walk_backward", {"vx": -0.15, "sim_duration_s": 2.0}),
        )

    def test_negation_never_executes(self):
        with self.assertRaises(ProposalError) as ctx:
            self.parser.parse("不要翻跟头")
        self.assertEqual(ctx.exception.classification, "parse_error")

    def test_conditional_rejected(self):
        for text in ("如果有人挡路就停下", "当地板滑的时候不要走"):
            with self.assertRaises(ProposalError) as ctx:
                self.parser.parse(text)
            self.assertEqual(ctx.exception.classification, "parse_error", text)

    def test_distance_not_rewritten(self):
        with self.assertRaises(ProposalError) as ctx:
            self.parser.parse("走三十厘米")
        self.assertEqual(ctx.exception.classification, "capability_gap")
        with self.assertRaises(ProposalError):
            self.parser.parse("走两分钟")

    def test_multistep_rejected_as_whole(self):
        for text in ("先蹲下再站起来", "蹲下然后起来", "左踢然后右踢"):
            with self.assertRaises(ProposalError) as ctx:
                self.parser.parse(text)
            self.assertEqual(ctx.exception.classification, "policy_rejected", text)

    def test_generic_dance_not_autobound(self):
        with self.assertRaises(ProposalError) as ctx:
            self.parser.parse("跳个舞")
        self.assertEqual(ctx.exception.classification, "capability_gap")

    def test_unknown_phrase(self):
        with self.assertRaises(ProposalError) as ctx:
            self.parser.parse("游泳")
        self.assertEqual(ctx.exception.classification, "capability_gap")

    def test_proposal_rejects_multiple_steps(self):
        with self.assertRaises(ProposalError) as ctx:
            new_proposal(
                steps=[Step("walk", {}), Step("stand", {})], raw_text="x", source="rule"
            )
        self.assertEqual(ctx.exception.classification, "policy_rejected")

    def test_proposal_rejects_real_target(self):
        with self.assertRaises(ProposalError) as ctx:
            new_proposal(steps=[Step("walk", {})], raw_text="x", source="rule", target="real")
        self.assertEqual(ctx.exception.classification, "policy_rejected")
        with self.assertRaises(ProposalError):
            proposal_from_llm_payload(
                json.dumps({"target": "real", "steps": [{"skill": "walk", "params": {}}]}),
                raw_text="x",
            )

    def test_proposal_expiry(self):
        proposal = make_proposal("walk", {"vx": 0.1})
        proposal.created_at -= 120  # age it past the 60 s TTL
        with self.assertRaises(ProposalError) as ctx:
            validate_proposal(proposal)
        self.assertIn("expired", ctx.exception.reason)
        gateway = Gateway(load_registry())
        result = gateway.check(proposal, execute=False)
        self.assertFalse(result.allowed)


class KimiClientTests(unittest.TestCase):
    def test_provider_error_classified(self):
        client = KimiClient(base_url="https://example.invalid", api_key="test-key")
        with mock.patch(
            "language_control.intent.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaises(ProposalError) as ctx:
                client.parse("向前走")
        self.assertEqual(ctx.exception.classification, "provider_error")

    def test_invalid_json_classified(self):
        client = KimiClient(base_url="https://example.invalid", api_key="test-key")

        class FakeResponse:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch(
            "language_control.intent.urllib.request.urlopen", return_value=FakeResponse()
        ):
            with self.assertRaises(ProposalError) as ctx:
                client.parse("向前走")
        self.assertEqual(ctx.exception.classification, "parse_error")

    def test_strict_schema_fallback(self):
        client = KimiClient(base_url="https://example.invalid", api_key="test-key")
        calls = []

        class FakeResponse:
            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": '{"target":"sim","steps":[{"skill":"stand","params":{}}]}'}}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=0):
            calls.append(json.loads(request.data.decode()))
            if len(calls) == 1:
                import urllib.error

                raise urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {}, None
                )
            return FakeResponse()

        with mock.patch(
            "language_control.intent.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            proposal = client.parse("站立")
        self.assertEqual(proposal.steps[0].skill, "stand")
        self.assertFalse(client.strict_schema_supported)
        self.assertEqual(calls[0]["response_format"]["type"], "json_schema")
        self.assertEqual(calls[1]["response_format"]["type"], "json_object")

    def test_unknown_skill_from_llm_is_capability_gap(self):
        payload = json.dumps({"target": "sim", "steps": [{"skill": "fly", "params": {}}]})
        proposal = proposal_from_llm_payload(payload, raw_text="飞")
        gateway = Gateway(load_registry())
        result = gateway.check(proposal, execute=False)
        self.assertFalse(result.allowed)
        self.assertEqual(result.classification, "capability_gap")


class SuggestionTests(unittest.TestCase):
    """"你是不是想说"建议层: 带时长后缀的输入剥后缀再匹配."""

    def test_duration_suffix_stripped_before_matching(self):
        from language_control.intent import suggest_phrases
        suggestions = suggest_phrases("向后推两秒")
        self.assertIn("向后走", suggestions)
        self.assertNotIn("捡东西", suggestions)


class GroundedPromptTests(unittest.TestCase):
    """2026-11-01: Kimi 技能匹配的注册表接地 prompt + 多步提案."""

    def test_prompt_mirrors_registry(self):
        from language_control.intent import build_system_prompt
        registry = load_registry()
        prompt = build_system_prompt(registry)
        # 技能面板同一份数据: 新技能与中文名自动进入
        self.assertIn("walk_backward", prompt)
        self.assertIn("后退", prompt)
        # 不可用技能标注诚实拒绝
        self.assertIn("NOT available", prompt)
        crouch_line = next(l for l in prompt.splitlines() if l.startswith("- crouch"))
        self.assertIn("NOT available", crouch_line)
        # 多步序列 + 参数规则
        self.assertIn("1-8 steps", prompt)
        self.assertIn("sim_duration_s", prompt)

    def test_llm_payload_multi_step(self):
        payload = json.dumps({
            "target": "sim",
            "steps": [
                {"skill": "walk", "params": {"vx": 0.15, "sim_duration_s": 2.0}},
                {"skill": "stand", "params": {}},
            ],
        })
        proposal = proposal_from_llm_payload(payload, raw_text="走两秒然后站住")
        self.assertEqual(len(proposal.steps), 2)
        self.assertEqual(proposal.steps[1].skill, "stand")


class GatewayLimitTests(unittest.TestCase):
    """三类编码各自正确且不互通限值."""

    def setUp(self):
        self.registry = load_registry()
        self.gateway = Gateway(self.registry)

    def test_velocity_limits_enforced(self):
        ok = self.gateway.check(make_proposal("walk", {"vx": 0.4, "vy": -0.3, "yaw": 1.0}), execute=False)
        self.assertTrue(ok.allowed, ok.reason)
        for params in ({"vx": 0.41}, {"vy": -0.31}, {"yaw": 1.01}, {"vx": -0.5}):
            result = self.gateway.check(make_proposal("walk", params), execute=False)
            self.assertFalse(result.allowed, params)
            self.assertEqual(result.classification, "policy_rejected")

    def test_posture_flag_ignores_speed_limits(self):
        # speed params are rejected outright, not clamped to velocity limits
        result = self.gateway.check(make_proposal("sitdown", {"vx": 0.1}), execute=False)
        self.assertFalse(result.allowed)
        self.assertIn("posture_flag", result.reason)
        # the contracted flag itself is fine
        ok = self.gateway.check(make_proposal("sitdown", {}), execute=False)
        self.assertTrue(ok.allowed, ok.reason)

    def test_phase_encoding_takes_no_velocity(self):
        result = self.gateway.check(make_proposal("ground_pick", {"vx": 0.1}), execute=False)
        self.assertFalse(result.allowed)
        ok = self.gateway.check(make_proposal("ground_pick", {}), execute=False)
        self.assertTrue(ok.allowed, ok.reason)

    def test_constant_encoding_takes_no_command(self):
        result = self.gateway.check(make_proposal("roulade", {"vx": 0.1}), execute=False)
        self.assertFalse(result.allowed)
        ok = self.gateway.check(make_proposal("roulade", {}), execute=False)
        self.assertTrue(ok.allowed, ok.reason)

    def test_usage_state_gating(self):
        # registered + execute -> refused (stand is registered+unknown)
        result = self.gateway.check(make_proposal("stand", {}), execute=True)
        self.assertFalse(result.allowed)
        # registered + dry-run -> allowed as dry-run
        ok = self.gateway.check(make_proposal("stand", {}), execute=False)
        self.assertTrue(ok.allowed)
        self.assertTrue(ok.dry_run_only)
        # available (post-M0-B) executes without extra flags
        avail = self.gateway.check(make_proposal("walk", {"vx": 0.1}), execute=True)
        self.assertTrue(avail.allowed, avail.reason)
        self.assertFalse(avail.dry_run_only)
        # experimental + failed: plain mode refused, --experimental alone still
        # refused (failed evidence), both flags -> allowed with labels retained
        plain = self.gateway.check(make_proposal("dance_side_step", {}), execute=True)
        self.assertFalse(plain.allowed)
        exp_only = Gateway(self.registry, experimental=True)
        self.assertFalse(exp_only.check(make_proposal("dance_side_step", {}), execute=True).allowed)
        both = Gateway(self.registry, experimental=True, research=True)
        allowed = both.check(make_proposal("dance_side_step", {}), execute=True)
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.labels.get("research_replay"))
        self.assertTrue(allowed.labels.get("experimental"))

    def test_pending_research_replay_labelled(self):
        plain = self.gateway.check(make_proposal("sitdown", {}), execute=True)
        self.assertFalse(plain.allowed)
        self.assertEqual(plain.classification, "policy_rejected")
        research = Gateway(self.registry, research=True)
        allowed = research.check(make_proposal("sitdown", {}), execute=True)
        self.assertTrue(allowed.allowed, allowed.reason)
        self.assertTrue(allowed.labels.get("research_replay"))
        self.assertEqual(allowed.labels.get("evidence_state"), "pending")

    def test_unknown_research_replay_is_bootstrap_path(self):
        # M0-B verification replays of registered+unknown skills run under
        # an explicit research flag, with the label retained.
        # (crouch: registered+unknown since stand was verified on 2026-11-01)
        plain = self.gateway.check(make_proposal("crouch", {}), execute=True)
        self.assertFalse(plain.allowed)
        research = Gateway(self.registry, research=True)
        allowed = research.check(make_proposal("crouch", {}), execute=True)
        self.assertTrue(allowed.allowed, allowed.reason)
        self.assertTrue(allowed.labels.get("research_replay"))
        self.assertEqual(allowed.labels.get("evidence_state"), "unknown")


class AdapterConstructionTests(unittest.TestCase):
    """dry-run 命令构造,三类编码各覆盖."""

    def setUp(self):
        self.registry = load_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, skill: str) -> dict:
        record = dict(self.registry["skills"][skill])
        record["run_id"] = "test-run"
        return record

    def test_velocity_construction(self):
        template = build_template("walk", self.record("walk"), {"vx": 0.15}, self.work_dir)
        argv = template.argv
        self.assertIn("play_isaac_velocity.py", argv[2])
        self.assertIn("--command-vx", argv)
        self.assertEqual(argv[argv.index("--command-vx") + 1], "0.15")
        self.assertIn("velstand.onnx", argv[argv.index("--policy-onnx") + 1])
        self.assertNotIn("--phase-clock", argv)
        self.assertEqual(template.expected["steps"], 100)
        self.assertAlmostEqual(template.sim_duration_s, 2.0)
        # secrets never enter the subprocess environment
        for key in template.env:
            self.assertNotRegex(key, r"(?i)(KEY|SECRET|TOKEN)")

    def test_phase_construction_uses_period_times_end_phase(self):
        record = self.record("ground_pick")
        self.assertEqual(phase_steps(record), 140)  # 4.0 x 0.7 x 50 Hz
        template = build_template("ground_pick", record, {}, self.work_dir)
        argv = template.argv
        self.assertEqual(argv[argv.index("--phase-clock") + 1], repr(4.0))
        self.assertEqual(argv[argv.index("--steps") + 1], "140")
        # duration_s (2.8) must not be passed as the clock period
        self.assertNotIn("2.8", argv)

    def test_posture_flag_construction(self):
        template = build_template("sitdown", self.record("sitdown"), {}, self.work_dir)
        argv = template.argv
        self.assertEqual(argv[argv.index("--command-vx") + 1], "1.0")  # sit flag
        self.assertNotIn("--phase-clock", argv)
        standup = build_template("standup", self.record("standup"), {}, self.work_dir)
        self.assertEqual(standup.argv[standup.argv.index("--command-vx") + 1], "0.0")

    def test_constant_construction_zeroes_command(self):
        template = build_template("roulade", self.record("roulade"), {}, self.work_dir)
        argv = template.argv
        self.assertEqual(argv[argv.index("--command-vx") + 1], "0.0")
        self.assertEqual(argv[argv.index("--steps") + 1], "50")  # 1.0 s x 50 Hz

    def test_mujoco_baseline_velocity_only(self):
        template = adapters.ADAPTERS["mujoco_baseline"].build(
            "walk", self.record("walk"), {"vx": 0.1}, self.work_dir
        )
        self.assertIn("run_mujoco_baseline.py", template.argv[1])
        record = self.record("ground_pick")
        with self.assertRaises(ValueError):
            adapters.ADAPTERS["mujoco_baseline"].build("ground_pick", record, {}, self.work_dir)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_constructs_only(self):
        executor = Executor()
        template = stub_template(["/bin/true"])
        result = executor.submit("t1", template, execute=False)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["command_constructed"])
        self.assertIsNone(result["process_completed"])
        self.assertEqual(result["action_evidence"], "unverified")
        self.assertEqual(executor.state, "idle")

    def test_cancel_during_starting(self):
        executor = Executor()
        template = stub_template(
            [sys.executable, "-c",
             "import time; time.sleep(5); print('READY', flush=True); time.sleep(30)"],
            marker="READY",
        )
        ack = executor.submit("t2", template, execute=True)
        self.assertTrue(ack["accepted"])
        pid = ack["pid"]
        self.assertEqual(executor.state, "starting")
        outcome = executor.cancel()
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(executor.state, "cancelled")
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)  # process group confirmed gone

    def test_cancel_during_running(self):
        executor = Executor()
        template = stub_template(
            [sys.executable, "-c",
             "import time; print('READY', flush=True); time.sleep(30)"],
            marker="READY",
        )
        ack = executor.submit("t3", template, execute=True)
        pid = ack["pid"]
        wait_state(executor, ("running",))
        outcome = executor.cancel()
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(executor.state, "cancelled")
        with self.assertRaises((ProcessLookupError, PermissionError)):
            os.killpg(pid, 0)

    def test_duplicate_submission_refused(self):
        executor = Executor()
        template = stub_template(
            [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(10)"],
            marker="READY",
        )
        first = executor.submit("t4", template, execute=True)
        self.assertTrue(first["accepted"])
        second = executor.submit("t4", template, execute=True)
        self.assertFalse(second["accepted"])
        self.assertIn("duplicate", second["reason"])
        executor.cancel()
        self.assertEqual(executor.state, "cancelled")

    def test_exit_zero_missing_result_not_success(self):
        executor = Executor()
        missing = self.tmp_path / "never_written.json"
        template = stub_template(
            [sys.executable, "-c", "print('READY')"],
            marker="READY",
            output=str(missing),
            expected={"steps": 5},
        )
        executor.submit("t5", template, execute=True)
        wait_state(executor, ("done", "failed"))
        result = executor.result
        self.assertTrue(result["process_completed"])  # exit 0, marker seen
        self.assertEqual(result["action_evidence"], "missing")
        self.assertEqual(result["state"], "failed")  # but NOT a success
        self.assertIn("not a success", result["overall"])

    def test_result_match_is_success(self):
        executor = Executor()
        output = self.tmp_path / "out.json"
        template = stub_template(
            [sys.executable, "-c",
             f"import json; print('READY', flush=True); "
             f"open(r'{output}', 'w').write(json.dumps({{'steps': 5}}))"],
            marker="READY",
            output=str(output),
            expected={"steps": 5},
        )
        executor.submit("t6", template, execute=True)
        wait_state(executor, ("done", "failed"))
        self.assertEqual(executor.state, "done")
        self.assertEqual(executor.result["action_evidence"], "passed")

    def test_result_mismatch_not_success(self):
        executor = Executor()
        output = self.tmp_path / "out.json"
        template = stub_template(
            [sys.executable, "-c",
             f"import json; print('READY', flush=True); "
             f"open(r'{output}', 'w').write(json.dumps({{'steps': 6}}))"],
            marker="READY",
            output=str(output),
            expected={"steps": 5},
        )
        executor.submit("t7", template, execute=True)
        wait_state(executor, ("done", "failed"))
        self.assertEqual(executor.result["action_evidence"], "mismatch")
        self.assertEqual(executor.state, "failed")


class LedgerTests(unittest.TestCase):
    def test_chain_integrity_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "events.jsonl")
            ledger.append("intent", raw_text="向前走", parsed_skill="walk")
            ledger.append("proposal", proposal_id="p1", steps=[{"skill": "walk", "params": {}}])
            ledger.append("gate", proposal_id="p1", allowed=True)
            ledger.append("execution", task_id="p1", dry_run=True)
            ledger.append("cancel", task_id="p1", stage="cancel_requested")
            ledger.append("capability-gap", raw_text="跳个舞", reason="x")
            self.assertEqual(ledger.verify(), [])
            # tamper detection
            path = ledger.path
            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[1])
            event["proposal_id"] = "tampered"
            lines[1] = json.dumps(event)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertNotEqual(ledger.verify(), [])

    def test_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "events.jsonl")
            ledger.append(
                "execution",
                env={"MOONSHOT_API_KEY": "sk-secret", "PATH": "/usr/bin"},
                note="Authorization: Bearer sk-secret",
            )
            event = list(foot_structure_ledger.iter_events(ledger.path))[0]
            self.assertEqual(event["env"], "<redacted>")
            self.assertNotIn("sk-secret", json.dumps(event))


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_app(self, **kwargs) -> LanguageControlApp:
        return LanguageControlApp(
            registry_path=REGISTRY_PATH,
            ledger_path=self.tmp_path / "events.jsonl",
            work_dir=self.tmp_path,
            **kwargs,
        )

    def test_dry_run_end_to_end(self):
        app = self.make_app()
        result = app.handle_text("向前走")
        self.assertEqual(result["handled"], "submitted")
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["command_constructed"])
        events = list(foot_structure_ledger.iter_events(app.ledger.path))
        types = [event["type"] for event in events]
        self.assertEqual(types, ["intent", "proposal", "gate", "execution"])
        self.assertEqual(app.ledger.verify(), [])

    def test_error_classes_recorded_distinctly(self):
        app = self.make_app()
        r1 = app.handle_text("走三十厘米")
        self.assertEqual(r1["classification"], "capability_gap")
        r2 = app.handle_text("不要翻跟头")
        self.assertEqual(r2["classification"], "parse_error")
        r3 = app.handle_text("先蹲下再站起来")
        self.assertEqual(r3["classification"], "policy_rejected")
        events = list(foot_structure_ledger.iter_events(app.ledger.path))
        classes = [e.get("classification") for e in events if e["type"] == "intent"]
        self.assertEqual(classes, ["capability_gap", "parse_error", "policy_rejected"])
        gaps = [e for e in events if e["type"] == "capability-gap"]
        self.assertEqual(len(gaps), 1)

    def test_registered_execute_refused_without_flags(self):
        app = self.make_app(execute=True)
        result = app.handle_text("翻跟头")  # roulade: registered+unknown
        self.assertEqual(result["handled"], "rejected")
        self.assertEqual(result["classification"], "policy_rejected")

    def test_research_replay_label_in_ledger(self):
        app = self.make_app(execute=True, research=True)
        result = app.handle_text("坐下")
        self.assertEqual(result["handled"], "submitted")
        self.assertTrue(result["labels"].get("research_replay"))
        app.executor.cancel()
        events = list(foot_structure_ledger.iter_events(app.ledger.path))
        gate = [e for e in events if e["type"] == "gate"][0]
        self.assertTrue(gate["labels"].get("research_replay"))

    def test_stop_is_local(self):
        app = self.make_app()
        with mock.patch.object(app, "llm", create=True, new=None):
            result = app.handle_text("停止")
        self.assertEqual(result["handled"], "stop")
        self.assertFalse(result["cancelled"])  # nothing running

    def test_unknown_skill_capability_gap_logged(self):
        app = self.make_app()
        result = app.handle_text("游泳")
        self.assertEqual(result["classification"], "capability_gap")
        events = list(foot_structure_ledger.iter_events(app.ledger.path))
        self.assertTrue(any(e["type"] == "capability-gap" for e in events))


if __name__ == "__main__":
    unittest.main()
