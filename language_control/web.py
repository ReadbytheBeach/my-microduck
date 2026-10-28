#!/usr/bin/env python3
"""Local thin web client for the session control service (M1 Web stage).

stdlib http.server only; binds 127.0.0.1; per-server session token;
request body limit. The web layer is a *client*: it writes request files
and reads responses/status exactly like mdctl — it never writes the command
file and never builds a second executor. Stop has its own handler path and
never waits on proposal parsing or the LLM.

三个独立状态区域: 提案/网关 · 执行进度 · 动作结果。GET /api/status 只读
文件,不阻塞;POST /api/stop 独立处理。本地原型,不宣传为生产安全方案。

Run: python -m language_control.web --port 8787  (token printed to stdout)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import playback_command_feed as pcf  # noqa: E402

from .intent import SequenceParser, STOP_PHRASES  # noqa: E402
from .proposal import ProposalError  # noqa: E402
from .mdctl import _wait_response, _write_request, session_dir  # noqa: E402
from .session_service import DEFAULT_REGISTRY, DEFAULT_SESSIONS_ROOT  # noqa: E402

MAX_BODY_BYTES = 64 * 1024

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>MicroDuck 语言控制</title>
<style>
body{font-family:system-ui;margin:2em;max-width:960px}
textarea{width:100%;height:3em}
.panel{border:1px solid #ccc;border-radius:8px;padding:1em;margin:1em 0}
.state-ok{color:#0a0}.state-warn{color:#c80}.state-bad{color:#c00}.state-na{color:#888}
#stop{background:#c00;color:#fff;font-size:1.2em;padding:.5em 2em;border:0;border-radius:8px}
pre{background:#f6f6f6;padding:.5em;overflow:auto;max-height:16em}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:2px 8px}
</style>
<h1>MicroDuck 语言控制（本地原型）</h1>
<div class=panel>
  <b>提案/网关</b><br>
  <textarea id=text placeholder="向前走两秒，然后停下"></textarea><br>
  <button onclick="say()">发送(规则解析)</button>
  <span id=gate></span>
</div>
<div class=panel>
  <b>执行进度</b> <button id=stop onclick="stop()">■ 停止</button>
  <pre id=progress>—</pre>
</div>
<div class=panel><b>动作结果</b><pre id=result>—</pre></div>
<div class=panel><b>技能面板</b><div id=skills>—</div></div>
<div class=panel><b>历史(原文→提案→网关→三判断)</b><pre id=history>—</pre></div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const SESSION = new URLSearchParams(location.search).get("session") || "sim01";
async function api(path, body){
  const opt = body ? {method:"POST",headers:{"Content-Type":"application/json",
    "X-Session-Token":TOKEN},body:JSON.stringify(body)} :
    {headers:{"X-Session-Token":TOKEN}};
  const r = await fetch(path,opt); return await r.json();
}
function cls(s){return {passed:"state-ok",completed:"state-ok",running:"state-ok",
  pending:"state-warn",unknown:"state-warn",applied:"state-warn",
  failed:"state-bad",rejected:"state-bad",fault_stopped:"state-bad"}[s]||"state-na"}
async function refresh(){
  try{
    const st = await api("/api/status?session="+SESSION);
    document.getElementById("progress").textContent = JSON.stringify(st,null,1);
    const sk = await api("/api/skills?session="+SESSION);
    let rows = "<tr><th>skill</th><th>usage</th><th>evidence</th><th>可执行</th></tr>";
    for(const [k,v] of Object.entries(sk.skills||{}))
      rows += `<tr><td>${k}</td><td class=${cls(v.usage_state)}>${v.usage_state}</td>`+
        `<td class=${cls(v.evidence_state)}>${v.evidence_state}</td>`+
        `<td class=${cls(v.session_executable?"passed":"failed")}>${v.session_executable?"✓":"✗"}</td></tr>`;
    document.getElementById("skills").innerHTML = "<table>"+rows+"</table>";
    const h = await api("/api/history?n=30");
    document.getElementById("history").textContent = h.lines.join("\\n");
  }catch(e){}
}
async function say(){
  const text = document.getElementById("text").value;
  const r = await api("/api/say",{session:SESSION,text:text,wait_result:true});
  document.getElementById("gate").textContent = JSON.stringify(r.response||r);
  document.getElementById("result").textContent = JSON.stringify(r,null,1);
  refresh();
}
async function stop(){
  const r = await api("/api/stop",{session:SESSION,mode:"stop_skill"});
  document.getElementById("result").textContent = JSON.stringify(r,null,1);
  refresh();
}
refresh(); setInterval(refresh, 2000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "microduck-web/1"

    # -- plumbing -------------------------------------------------------------
    def _send(self, code: int, payload, content_type="application/json") -> None:
        body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.headers.get("X-Session-Token", "")
        if token and secrets.compare_digest(token, self.server.token):
            return True
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        given = query.get("token", [""])[0]
        return bool(given) and secrets.compare_digest(given, self.server.token)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return None

    def log_message(self, *args):  # keep test output clean
        pass

    # -- GET --------------------------------------------------------------------
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if not self._authorized():
            self._send(403, {"error": "bad session token"})
            return
        name = query.get("session", ["sim01"])[0]
        sdir = session_dir(name, self.server.sessions_root)
        if parsed.path == "/api/status":
            meta = pcf.read_json(sdir / "session.json")
            status = pcf.read_json(sdir / "status.json")
            ack = pcf.read_json(sdir / "ack.json")
            fresh = bool(status) and (
                time.time() - float(status.get("wall_updated_at", 0.0)) < 3.0)
            self._send(200, {
                "meta": meta, "status": status, "snapshot_fresh": fresh,
                "recent_acks": (ack or {}).get("acks", [])[-5:],
                "results": (ack or {}).get("results", {}),
            })
            return
        if parsed.path == "/api/skills":
            registry = json.loads(
                (self.server.registry_path).read_text(encoding="utf-8"))
            meta = pcf.read_json(sdir / "session.json") or {}
            session_model = meta.get("policy_model")
            walk_ok = registry["skills"]["walk"]["usage_state"] == "available"
            skills = {}
            for skill, record in sorted(registry["skills"].items()):
                encoding = record.get("command", {}).get("encoding")
                model = (record.get("model") or {}).get("path")
                if record.get("kind") == "builtin":
                    executable = True
                elif encoding != "velocity":
                    executable = False
                elif skill == "stand" and session_model and model != session_model:
                    executable = walk_ok
                else:
                    executable = record.get("usage_state") == "available" and (
                        session_model is None or model == session_model)
                skills[skill] = {
                    "usage_state": record.get("usage_state"),
                    "evidence_state": record.get("evidence_state"),
                    "encoding": encoding,
                    "session_executable": executable,
                }
            self._send(200, {"skills": skills})
            return
        if parsed.path == "/api/history":
            count = int(query.get("n", ["30"])[0])
            lines: list[str] = []
            ledger = self.server.ledger_path
            if ledger.is_file():
                events = [json.loads(line) for line in
                          ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
                for event in events[-count:]:
                    kind = event.get("type")
                    if kind == "intent":
                        lines.append(f"原文 {event.get('raw_text')} -> {event.get('parsed_skill')}")
                    elif kind == "gate":
                        lines.append(f"  网关 allowed={event.get('allowed')} {event.get('reason','')}")
                    elif kind == "execution":
                        lines.append(f"  执行 {event.get('stage','dry_run')} {event.get('overall','')}")
                    elif kind in ("command", "ack", "sequence", "session", "fault"):
                        lines.append(f"  [{kind}] " + json.dumps(
                            {k: v for k, v in event.items()
                             if k not in ("type", "ts", "seq", "prev_hash", "event_hash")},
                            ensure_ascii=False)[:200])
            self._send(200, {"lines": lines})
            return
        self._send(404, {"error": "unknown path"})

    # -- POST -------------------------------------------------------------------
    def do_POST(self):
        from urllib.parse import urlparse
        if not self._authorized():
            self._send(403, {"error": "bad session token"})
            return
        body = self._read_body()
        if body is None:
            return
        name = body.get("session", "sim01")
        sdir = session_dir(name, self.server.sessions_root)
        if not (sdir / "session.json").is_file():
            self._send(404, {"error": f"session {name!r} not found"})
            return
        path = urlparse(self.path).path
        # 停止是独立路径: 不解析、不排队、不等 LLM
        if path == "/api/stop":
            mode = body.get("mode", "stop_skill")
            if mode not in ("stop_skill", "pause_sim", "cancel_session"):
                self._send(400, {"error": f"unknown stop mode {mode!r}"})
                return
            request_id = _write_request(sdir, {"op": "stop", "mode": mode})
            response = _wait_response(sdir, request_id, timeout_s=30.0)
            self._send(200, {"stopped": True, "mode": mode, "response": response})
            return
        if path == "/api/say":
            text = str(body.get("text", ""))
            if not text.strip():
                self._send(400, {"error": "empty text"})
                return
            normalized = "".join(text.split())
            if normalized in STOP_PHRASES:
                request_id = _write_request(sdir, {"op": "stop", "mode": "stop_skill"})
                response = _wait_response(sdir, request_id, timeout_s=30.0)
                self._send(200, {"handled": "stop", "response": response})
                return
            try:
                proposal = SequenceParser().parse(text)  # 本地规则,无 LLM
            except ProposalError as exc:
                self._send(200, {"handled": "rejected",
                                 "classification": exc.classification,
                                 "reason": exc.reason})
                return
            request_id = _write_request(sdir, {
                "op": "sequence", "source": "rule", "raw_text": text,
                "proposal_id": proposal.proposal_id,
                "steps": [step.to_dict() for step in proposal.steps],
            })
            wait_result = bool(body.get("wait_result", True))
            response = _wait_response(
                sdir, request_id, timeout_s=float(body.get("timeout_s", 120)),
                wait_result=wait_result)
            self._send(200, {"handled": "submitted", "response": response})
            return
        if path == "/api/run":
            skill = str(body.get("skill", ""))
            params = body.get("params", {})
            if not isinstance(params, dict):
                self._send(400, {"error": "params must be an object"})
                return
            request_id = _write_request(sdir, {
                "op": "run", "source": "web",
                "steps": [{"skill": skill, "params": params}],
            })
            response = _wait_response(
                sdir, request_id, timeout_s=float(body.get("timeout_s", 120)),
                wait_result=bool(body.get("wait_result", False)))
            self._send(200, {"response": response})
            return
        self._send(404, {"error": "unknown path"})


def make_server(
    port: int = 8787,
    registry_path: Path = DEFAULT_REGISTRY,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    ledger_path: Path | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token or secrets.token_urlsafe(24)
    server.registry_path = Path(registry_path)
    server.sessions_root = Path(sessions_root)
    server.ledger_path = Path(
        ledger_path or PROJECT_ROOT / "work/language_control/events.jsonl")
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    args = parser.parse_args(argv)
    server = make_server(args.port, args.registry, args.sessions_root)
    url = f"http://127.0.0.1:{args.port}/?token={server.token}"
    print(f"MicroDuck web client (local prototype): {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
