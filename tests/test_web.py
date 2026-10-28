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
