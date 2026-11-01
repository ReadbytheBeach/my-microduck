#!/usr/bin/env python3
"""Web thin-client tests (stdlib urllib against 127.0.0.1, stub session)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from language_control import web  # noqa: E402
from test_session_service import StubSession, wait_for  # noqa: E402


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)
        cls.session = StubSession(
            cls.tmp_path, name="web", streaming_lease_s=3600.0
        ).__enter__()
        cls.server = web.make_server(
            port=0,
            sessions_root=cls.tmp_path,  # StubSession dir is tmp/web
            ledger_path=cls.tmp_path / "events-web.jsonl",
        )
        # StubSession session dir is tmp_path/"web"; sessions_root must point at tmp_path
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = cls.server.token

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.session.__exit__(None, None, None)
        cls.tmp.cleanup()

    def call(self, path: str, body: dict | None = None, token: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        use_token = self.token if token is None else token
        if use_token:
            headers["X-Session-Token"] = use_token
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_page_served_without_token(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("MicroDuck", html)

    def test_token_required(self):
        code, payload = self.call("/api/status?session=web", token="wrong")
        self.assertEqual(code, 403)
        code, payload = self.call("/api/status?session=web", token="")
        self.assertEqual(code, 403)

    def test_body_limit(self):
        big = {"session": "web", "text": "x" * (128 * 1024)}
        code, _ = self.call("/api/say", big)
        self.assertEqual(code, 413)

    def test_status_and_skills(self):
        code, payload = self.call("/api/status?session=web")
        self.assertEqual(code, 200)
        self.assertTrue(payload["snapshot_fresh"])
        self.assertEqual(payload["meta"]["backend"], "stub")
        code, payload = self.call("/api/skills?session=web")
        self.assertEqual(code, 200)
        self.assertTrue(payload["skills"]["walk"]["session_executable"])
        self.assertFalse(payload["skills"]["crouch"]["session_executable"])
        # 中文名列 + 说法列（2026-11-01）；带参数的说法有方向/数值标注
        walk = payload["skills"]["walk"]
        self.assertEqual(walk["zh"], "行走")
        self.assertIn("向前走(前进0.15m/s)", walk["phrases"])
        self.assertIn("原地走", walk["phrases"])  # 无参数原样
        self.assertTrue(walk["model"].endswith("policies/v7/velstand.onnx"))
        self.assertIsNone(payload["skills"]["stop"]["model"])  # 内置技能无模型
        # 后退独立成技（2026-11-01）：自己的行/说法列/会话可执行
        wb = payload["skills"]["walk_backward"]
        self.assertEqual(wb["zh"], "后退")
        self.assertIn("向后走(后退0.15m/s)", wb["phrases"])
        self.assertNotIn("向后走(后退0.15m/s)", walk["phrases"])
        self.assertTrue(wb["session_executable"])

    def test_say_sequence_via_web(self):
        code, payload = self.call("/api/say", {
            "session": "web", "text": "向前走两秒，然后停下", "wait_result": True,
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "submitted")
        self.assertEqual(payload["response"]["status"], "completed")

    def test_stop_reachable_while_running(self):
        # 运行中停止:独立路径,快速返回
        code, payload = self.call("/api/run", {
            "session": "web", "skill": "walk", "params": {"vx": 0.1},
        })
        self.assertEqual(code, 200)
        start = time.monotonic()
        code, payload = self.call("/api/stop", {"session": "web", "mode": "stop_skill"})
        elapsed = time.monotonic() - start
        self.assertEqual(code, 200)
        self.assertTrue(payload["stopped"])
        self.assertLess(elapsed, 5.0)
        status = wait_for(
            lambda: (self.session.status()["current_velocity"] == [0.0, 0.0, 0.0]
                     and self.session.status()["run_state"] == "running"),
            what="velocity zeroed after stop",
        )
        self.assertTrue(status)

    def test_check_endpoint(self):
        command_existed_before = (self.tmp_path / "web" / "command.json").exists()
        # 绿: 可执行
        code, payload = self.call("/api/check", {
            "session": "web", "text": "向前走两秒",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "checked")
        self.assertEqual(payload["response"]["status"], "ok")
        # 黄/红: 解析通过但网关在会话侧拒绝(非 velocity 编码)
        code, payload = self.call("/api/check", {
            "session": "web", "text": "翻跟头",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["response"]["status"], "rejected")
        # 红: 规则解析直接失败
        code, payload = self.call("/api/check", {
            "session": "web", "text": "啊吧啊吧",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "rejected")
        # 停止短语: 提示立即执行,不投递
        code, payload = self.call("/api/check", {
            "session": "web", "text": "停下",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "stop_hint")
        # check 不产生任何执行痕迹: command.json 状态不变
        self.assertFalse(command_existed_before)
        self.assertFalse((self.tmp_path / "web" / "command.json").exists())

    def test_check_suggestions_on_reject(self):
        # 解析失败附"你是不是想说"建议（语音错字场景）
        code, payload = self.call("/api/check", {
            "session": "web", "text": "向后推",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "rejected")
        self.assertIn("向后走", payload["suggestions"])

    def test_use_llm_without_key_falls_back_to_rule(self):
        # 选中 Kimi 但无 key: provider_error 回退规则解析,结果标注 parser=rule
        import os
        from unittest import mock
        env = {k: v for k, v in os.environ.items()
               if k not in ("KIMI_LC_API_KEY", "MOONSHOT_API_KEY")}
        self.server.kimi = None
        with mock.patch.dict(os.environ, env, clear=True):
            code, payload = self.call("/api/check", {
                "session": "web", "text": "向前走两秒", "use_llm": True,
            })
        self.assertEqual(code, 200)
        self.assertEqual(payload["handled"], "checked")
        self.assertEqual(payload["parser"], "rule")
        self.assertEqual(payload["response"]["status"], "ok")
        self.server.kimi = None

    @unittest.skipUnless(
        __import__("os").environ.get("MOONSHOT_API_KEY")
        or __import__("os").environ.get("KIMI_LC_API_KEY"),
        "Kimi key not set",
    )
    def test_use_llm_live_kimi_parses(self):
        # 有 key 时选中 Kimi: 自由表述由模型理解,网关卡控不变
        self.server.kimi = None
        code, payload = self.call("/api/check", {
            "session": "web", "text": "麻烦你往前挪动两秒钟", "use_llm": True,
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload.get("parser"), "kimi")
        self.assertIn(payload["handled"], ("checked", "rejected"))
        # "先蹲下再站起来" 应诚实拒(两技能 registered),不猜不放行
        code, payload = self.call("/api/check", {
            "session": "web", "text": "先蹲下再站起来", "use_llm": True,
        })
        self.assertEqual(payload["handled"], "rejected")
        self.server.kimi = None

    def test_kimi_key_config_in_memory_only(self):
        secret = "sk-test-abcdef1234567890"
        # 未配置时的状态
        code, payload = self.call("/api/config/kimi-key")
        self.assertEqual(code, 200)
        self.assertFalse(payload["configured"])
        # 设置（无需会话）→ 状态可见尾号
        code, payload = self.call("/api/config/kimi-key", {"key": secret})
        self.assertEqual(code, 200)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["tail"], "7890")
        code, payload = self.call("/api/config/kimi-key")
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["source"], "page")
        # key 不落盘、不入账本
        self.assertNotIn(secret, (self.tmp_path / "events-web.jsonl").read_text(
            encoding="utf-8"))
        for leaked in self.tmp_path.rglob("*.json"):
            self.assertNotIn(secret, leaked.read_text(encoding="utf-8"))
        # 空串清除
        code, payload = self.call("/api/config/kimi-key", {"key": ""})
        self.assertFalse(payload["configured"])

    def test_clip_and_frame_endpoints(self):
        code, payload = self.call("/api/clips?session=web")
        self.assertEqual(code, 200)
        self.assertIn("clips", payload)
        self.assertTrue(payload["dir"].endswith("/web/clips"))
        # 路径穿越防护
        code, _ = self.call("/api/clip?session=web&file=..%2Fsecret.mp4")
        self.assertEqual(code, 400)
        code, _ = self.call("/api/clip?session=web&file=nope.mp4")
        self.assertEqual(code, 404)
        # stub 会话无预览帧
        code, _ = self.call("/api/frame.jpg?session=web")
        self.assertEqual(code, 404)

    def call_raw(self, path: str, data: bytes, content_type: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": content_type, "X-Session-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_voice_endpoint_transcribes_and_ledgers(self):
        from language_control.asr import DEFAULT_MODEL_DIR
        wav = DEFAULT_MODEL_DIR / "test_wavs" / "0.wav"
        if not wav.is_file():
            self.skipTest("ASR model/test wav not downloaded")
        code, payload = self.call_raw(
            "/api/voice?session=web", wav.read_bytes(), "audio/wav")
        self.assertEqual(code, 200)
        self.assertIn("研究", payload["text"])
        self.assertEqual(len(payload["model_sha256"]), 64)
        ledger = (self.tmp_path / "events-web.jsonl").read_text(encoding="utf-8")
        self.assertIn('"asr"', ledger)
        # 音频默认不留存
        self.assertFalse(list((self.tmp_path / "web" / "requests").glob("voice-*")))

    def test_voice_rejects_oversize_body(self):
        code, payload = self.call_raw(
            "/api/voice?session=web", b"x" * (9 * 1024 * 1024), "audio/webm")
        self.assertEqual(code, 413)

    def test_sessions_listing(self):
        code, payload = self.call("/api/sessions")
        self.assertEqual(code, 200)
        names = [s["name"] for s in payload["sessions"]]
        self.assertIn("web", names)
        entry = next(s for s in payload["sessions"] if s["name"] == "web")
        self.assertEqual(entry["backend"], "stub")

    def test_history_endpoint(self):
        code, payload = self.call("/api/history?n=10")
        self.assertEqual(code, 200)
        self.assertIn("lines", payload)

    def test_unknown_session_404(self):
        code, _ = self.call("/api/status?session=nope")
        self.assertEqual(code, 200)  # status of missing session: meta null
        code, payload = self.call("/api/stop", {"session": "nope", "mode": "stop_skill"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
