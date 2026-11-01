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

from .intent import (  # noqa: E402
    SKILL_ZH,
    KimiClient,
    SequenceParser,
    STOP_PHRASES,
    skill_phrase_variants,
    suggest_phrases,
)
from .proposal import ProposalError  # noqa: E402
from .mdctl import _wait_response, _write_request, session_dir  # noqa: E402
from .session_service import DEFAULT_REGISTRY, DEFAULT_SESSIONS_ROOT  # noqa: E402

MAX_BODY_BYTES = 64 * 1024
MAX_AUDIO_BODY_BYTES = 8 * 1024 * 1024


def _env_kimi_key() -> str:
    import os
    return os.environ.get("KIMI_LC_API_KEY") or os.environ.get("MOONSHOT_API_KEY", "")

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>MicroDuck 语言控制</title>
<style>
body{font-family:system-ui;margin:2em;max-width:1600px}
textarea{width:100%;height:7em;font-size:1.05em}
.panel{border:1px solid #ccc;border-radius:8px;padding:1em;margin:1em 0}
.row{display:flex;gap:1em;align-items:stretch}
.row>.panel{flex:1;min-width:0;margin:0}
.row{margin:1em 0}
.state-ok{color:#0a0}.state-warn{color:#c80}.state-bad{color:#c00}.state-na{color:#888}
#stop{background:#c00;color:#fff;font-size:1.2em;padding:.5em 2em;border:0;border-radius:8px}
#sessName{width:24em}
pre{background:#f6f6f6;padding:.5em;overflow:auto;max-height:16em}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:2px 8px}
#preview,video{max-width:480px}
</style>
<h1>MicroDuck 语言控制（本地原型）</h1>
<div class=panel>
  <b>会话</b> <input id=sessName> <button onclick="switchSession()">切换</button>
  <span id=sessState class=state-na>—</span>
</div>
<div class=row>
<div class=panel>
  <b>提案/网关</b><br>
  <textarea id=text placeholder="向前走两秒，然后停下" oninput="scheduleCheck()"></textarea><br>
  <button onclick="configKimiKey()">export MOONSHOT_API_KEY=...</button>
  <span id=kimiKeyState class=state-na></span><br>
  <button id=recBtn onclick="toggleRecord()">2.语音</button>
  <span id=heard></span><br>
  <button onclick="say()">3.发送并规则解析</button>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <label><input type=checkbox id=useLlm onchange="scheduleCheck()"> 1.选中则使用Kimi模型解析/或规则匹配</label><br>
  网关校验(规则判定): <span id=verdict class=state-na>—</span> <span id=suggest class=state-warn></span><br>
  <span id=matched class=state-ok></span><br>
  <span id=gate></span>
</div>
<div class=panel><b>技能面板</b>（✓=本会话可执行，照着拼指令）<div id=skills>—</div></div>
</div>
<div class=panel>
  <b>执行进度</b> <button id=stop onclick="stop()">■ 停止</button>
  <pre id=progress>—</pre>
</div>
<div class=panel>
  <b>仿真画面</b>（需会话以 --video 启动）<br>
  <img id=preview style="display:block" alt="无预览帧">
  <div id=clipsDir style="color:#888;font-size:.85em"></div>
  <div id=clips>—</div>
</div>
<div class=panel><b>动作结果</b><pre id=result>—</pre></div>
<div class=panel><b>历史(原文→提案→网关→三判断)</b><pre id=history>—</pre></div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
let SESSION = new URLSearchParams(location.search).get("session") || "";
function switchSession(){
  const v = document.getElementById("sessName").value.trim();
  if(!v || v === SESSION) return;
  SESSION = v;
  seenClips = "";  // 强制重渲染片段列表
  refresh(); refreshVideo();
}
async function pickSession(){
  if(SESSION) return;
  try{
    const r = await api("/api/sessions");
    const active = (r.sessions||[]).filter(s=>["running","paused","starting","fault"].includes(s.state));
    SESSION = (active[0] || (r.sessions||[])[0] || {}).name || "sim01";
  }catch(e){ SESSION = "sim01"; }
}
async function api(path, body){
  const opt = body ? {method:"POST",headers:{"Content-Type":"application/json",
    "X-Session-Token":TOKEN},body:JSON.stringify(body)} :
    {headers:{"X-Session-Token":TOKEN}};
  const r = await fetch(path,opt); return await r.json();
}
function cls(s){return {passed:"state-ok",completed:"state-ok",running:"state-ok",
  pending:"state-warn",unknown:"state-warn",applied:"state-warn",
  failed:"state-bad",rejected:"state-bad",fault_stopped:"state-bad"}[s]||"state-na"}
let mediaRecorder = null, audioChunks = [];
async function toggleRecord(){
  const btn = document.getElementById("recBtn");
  const heard = document.getElementById("heard");
  if(mediaRecorder && mediaRecorder.state === "recording"){
    mediaRecorder.stop();
    return;
  }
  try{
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      btn.textContent = "🎙 语音";
      heard.textContent = "转写中…";
      const blob = new Blob(audioChunks, {type: mediaRecorder.mimeType});
      try{
        const r = await fetch("/api/voice?session="+SESSION, {method: "POST",
          headers: {"X-Session-Token": TOKEN, "Content-Type": mediaRecorder.mimeType},
          body: blob});
        const data = await r.json();
        if(!data.text){ heard.textContent = data.error || "没听清，请重说"; return; }
        heard.textContent = "我听到的是：" + data.text;
        document.getElementById("text").value = data.text;
        const chk = await api("/api/check", {session: SESSION, text: data.text, use_llm: useLlm()});
        if(chk.handled === "stop_hint"){ stop(); return; }  // 停下:不等确认
        if(chk.handled === "rejected"){
          const v0 = document.getElementById("verdict");
          v0.textContent = "✗ 不可执行 (" + chk.classification + ")";
          v0.title = chk.reason || "";
          v0.className = "state-bad";
          showSuggest(chk.suggestions);
          return;
        }
        const resp = chk.response || {};
        const v = document.getElementById("verdict");
        if(resp.status === "ok"){ v.textContent = "✓ 可执行，确认后点发送("+parserTag(chk.parser)+")"; v.className = "state-ok"; showMatched(chk.matched); }
        else { v.textContent = "✗ 不可执行 (" + (resp.classification||"rejected") + ")"; v.title = resp.reason || ""; v.className = "state-bad"; showMatched(chk.matched); }
      }catch(e){ heard.textContent = "转写失败: " + e; }
    };
    mediaRecorder.start();
    btn.textContent = "■ 停止录音";
    heard.textContent = "录音中…";
  }catch(e){ heard.textContent = "麦克风不可用: " + e; }
}
let checkTimer = null;
function scheduleCheck(){
  const v = document.getElementById("verdict");
  if(checkTimer) clearTimeout(checkTimer);
  const text = document.getElementById("text").value;
  if(!text.trim()){ v.textContent="—"; v.className="state-na"; return; }
  checkTimer = setTimeout(() => checkText(text), 500);
}
function useLlm(){ return document.getElementById("useLlm").checked; }
async function refreshKimiKey(){
  try{
    const r = await api("/api/config/kimi-key");
    const el = document.getElementById("kimiKeyState");
    if(r.configured){ el.textContent = "key 已配置(页面,尾号"+r.tail+")"; el.className="state-ok"; }
    else if(r.source==="env"){ el.textContent = "key 来自环境变量"; el.className="state-ok"; }
    else { el.textContent = "未配置(勾选Kimi前请先点左键设置)"; el.className="state-warn"; }
  }catch(e){}
}
async function configKimiKey(){
  const input = window.prompt(
    "粘贴 Moonshot API Key（只存本 web 进程内存,不写盘不入账本；留空=清除）","");
  if(input === null) return;  // 取消
  const r = await api("/api/config/kimi-key", {key: input.trim()});
  refreshKimiKey();
}
function parserTag(p){ return p==="kimi" ? "Kimi理解" : "规则解析"; }
function formatMatched(list){
  if(!list || !list.length) return "";
  return "匹配结果: " + list.map(s=>{
    const p = Object.entries(s.params||{}).map(([k,v])=>k+"="+v).join(",");
    return s.skill + (p ? "("+p+")" : "");
  }).join(" → ");
}
function showMatched(list){
  document.getElementById("matched").textContent = formatMatched(list);
}
function showSuggest(list){
  document.getElementById("suggest").textContent =
    (list && list.length) ? "你是不是想说："+list.join(" / ") : "";
}
async function checkText(text){
  const v = document.getElementById("verdict");
  v.title = "";
  showSuggest(null); showMatched(null);
  try{
    const r = await api("/api/check",{session:SESSION,text:text,use_llm:useLlm()});
    if(r.error){ v.textContent="会话未起"; v.className="state-na"; return; }
    if(r.handled==="stop_hint"){ v.textContent="停下:立即执行"; v.className="state-ok"; return; }
    if(r.handled==="rejected"){
      v.textContent="✗ 不可执行 ("+r.classification+")"; v.className="state-bad";
      v.title = r.reason || "";  // 长原因收进悬停提示,详情见动作结果区
      showSuggest(r.suggestions); return;
    }
    const resp = r.response||{};
    if(resp.status==="ok"){
      const n=(resp.steps||[]).length;
      v.textContent="✓ 可执行 ("+n+"步,"+parserTag(r.parser)+")"; v.className="state-ok";
      showMatched(r.matched);
    }else{
      v.textContent="✗ 不可执行 ("+(resp.classification||"rejected")+")";
      v.className = resp.status==="rejected" ? "state-bad" : "state-warn";
      v.title = resp.reason || "";
      showMatched(r.matched);  // 网关拒了也要看见 Kimi 匹配成了什么
    }
  }catch(e){ v.textContent="校验不可用"; v.className="state-na"; }
}
let seenClips = "";
async function refreshVideo(){
  try{
    const img = document.getElementById("preview");
    img.src = "/api/frame.jpg?session="+SESSION+"&token="+TOKEN+"&t="+Date.now();
    const c = await api("/api/clips?session="+SESSION);
    document.getElementById("clipsDir").textContent = c.dir ? "片段目录: "+c.dir : "";
    const box = document.getElementById("clips");
    if(!c.clips || !c.clips.length){ box.textContent="（暂无片段）"; return; }
    const key = c.clips.map(x=>x.name).join(",");
    if(key === seenClips) return;  // 不重排 DOM,避免打断正在播放的片段
    seenClips = key;
    box.innerHTML = c.clips.map(x=>
      `<div title="${c.dir}/${x.name}">${x.name} `+
      `<video src="/api/clip?session=${SESSION}&file=${encodeURIComponent(x.name)}&token=${TOKEN}" `+
      `controls preload="none" style="display:block"></video></div>`).join("");
  }catch(e){}
}
async function refresh(){
  try{
    const st = await api("/api/status?session="+SESSION);
    document.getElementById("sessName").value = SESSION;
    const sst = document.getElementById("sessState");
    const mstate = (st.meta||{}).state;
    sst.textContent = mstate || "无此会话";
    sst.className = cls(mstate||"");
    document.getElementById("progress").textContent = JSON.stringify(st,null,1);
    const sk = await api("/api/skills?session="+SESSION);
    let rows = "<tr><th>skill</th><th>技能</th><th>说法</th><th>usage</th><th>evidence</th><th>可执行</th></tr>";
    for(const [k,v] of Object.entries(sk.skills||{}))
      rows += `<tr><td>${k}</td><td>${v.zh||""}</td>`+
        `<td style="font-size:.85em">${(v.phrases||[]).join("/")}</td>`+
        `<td class=${cls(v.usage_state)}>${v.usage_state}</td>`+
        `<td class=${cls(v.evidence_state)}>${v.evidence_state}</td>`+
        `<td class=${cls(v.session_executable?"passed":"failed")}>${v.session_executable?"✓":"✗"}</td></tr>`;
    document.getElementById("skills").innerHTML =
      '<div style="color:#888;font-size:.8em;margin-bottom:.5em">模型路径:<br>'+
      Object.entries(sk.skills||{}).filter(([k,v])=>v.model)
        .map(([k,v])=>`${k}: ${v.model}`).join("<br>")+"</div>"+
      "<table>"+rows+"</table>";
    const h = await api("/api/history?n=30");
    document.getElementById("history").textContent = h.lines.join("\\n");
  }catch(e){}
}
async function say(){
  const text = document.getElementById("text").value;
  const r = await api("/api/say",{session:SESSION,text:text,wait_result:true,use_llm:useLlm()});
  if(r.handled==="rejected") showSuggest(r.suggestions);
  showMatched(r.matched);
  document.getElementById("gate").textContent = JSON.stringify(r.response||r);
  document.getElementById("result").textContent = JSON.stringify(r,null,1);
  refresh();
}
async function stop(){
  const r = await api("/api/stop",{session:SESSION,mode:"stop_skill"});
  document.getElementById("result").textContent = JSON.stringify(r,null,1);
  refresh();
}
pickSession().then(() => { refresh(); refreshVideo(); refreshKimiKey();
  setInterval(refresh, 2000); setInterval(refreshVideo, 1000); });
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "microduck-web/1"

    # -- plumbing -------------------------------------------------------------
    def _send(self, code: int, payload, content_type="application/json") -> None:
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

    # -- parsing (rule / Kimi toggle) --------------------------------------
    def _kimi(self) -> KimiClient:
        """Lazy KimiClient with a registry-grounded system prompt, rebuilt
        whenever the registry file changes (skills panel == matching space).
        A key set via /api/config/kimi-key takes precedence over env vars."""
        from .intent import build_system_prompt
        if self.server.kimi is None:
            self.server.kimi = KimiClient(api_key=self.server.kimi_key)
        registry_path = self.server.registry_path
        mtime = registry_path.stat().st_mtime if registry_path.is_file() else None
        if getattr(self.server, "_kimi_prompt_mtime", None) != mtime:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            self.server.kimi.system_prompt = build_system_prompt(registry)
            self.server._kimi_prompt_mtime = mtime
        return self.server.kimi

    def _parse_text(self, text: str, use_llm: bool) -> tuple[object | None, dict | None, str]:
        """(proposal, error_payload, parser_used).

        use_llm 选中：Kimi 先解析；provider 故障（无 key/超时）回退规则解析,
        理解性拒绝（capability_gap 等）不回退——LLM 的判决就是答案。
        未选中：仅规则。两条路都失败时附"你是不是想说"建议。
        """
        kimi_error = None
        if use_llm:
            try:
                return self._kimi().parse(text), None, "kimi"
            except ProposalError as exc:
                if exc.classification != "provider_error":
                    return None, {
                        "handled": "rejected", "classification": exc.classification,
                        "reason": exc.reason, "parser": "kimi",
                        "suggestions": suggest_phrases(text),
                    }, "kimi"
                kimi_error = exc  # 无 key/超时等: 回退规则
        try:
            proposal = SequenceParser().parse(text)
            return proposal, None, "rule"
        except ProposalError as exc:
            payload = {
                "handled": "rejected", "classification": exc.classification,
                "reason": exc.reason, "parser": "rule",
                "suggestions": suggest_phrases(text),
            }
            if kimi_error is not None:
                payload["note"] = f"Kimi 不可用已回退规则解析: {kimi_error.reason}"
            return None, payload, "rule"

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
        if parsed.path == "/api/sessions":
            root = self.server.sessions_root
            sessions = []
            if root.is_dir():
                for child in root.iterdir():
                    meta_file = child / "session.json"
                    if not meta_file.is_file():
                        continue
                    meta = pcf.read_json(meta_file) or {}
                    sessions.append({
                        "name": child.name,
                        "state": meta.get("state"),
                        "backend": meta.get("backend"),
                        "video": bool(meta.get("video")),
                        "mtime": meta_file.stat().st_mtime,
                    })
            sessions.sort(key=lambda s: s["mtime"], reverse=True)
            self._send(200, {"sessions": sessions})
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
                    "zh": SKILL_ZH.get(skill, ""),
                    "phrases": skill_phrase_variants(skill),
                    "model": model,
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
        if parsed.path == "/api/clips":
            clips_dir = sdir / "clips"
            clips = []
            if clips_dir.is_dir():
                for f in sorted(clips_dir.glob("*.mp4")):
                    clips.append({"name": f.name, "bytes": f.stat().st_size})
            self._send(200, {"clips": clips, "dir": str(clips_dir.resolve())})
            return
        if parsed.path == "/api/clip":
            fname = query.get("file", [""])[0]
            if not fname or Path(fname).name != fname or not fname.endswith(".mp4"):
                self._send(400, {"error": "bad clip name"})
                return
            target = sdir / "clips" / fname
            if not target.is_file():
                self._send(404, {"error": "clip not found"})
                return
            self._send(200, target.read_bytes(), "video/mp4")
            return
        if parsed.path == "/api/frame.jpg":
            frame = sdir / "latest.jpg"
            if not frame.is_file():
                self._send(404, {"error": "no preview frame"})
                return
            self._send(200, frame.read_bytes(), "image/jpeg")
            return
        if parsed.path == "/api/config/kimi-key":
            key = self.server.kimi_key
            self._send(200, {
                "configured": bool(key),
                "tail": key[-4:] if key else "",
                "source": "page" if key else ("env" if _env_kimi_key() else "none"),
            })
            return
        self._send(404, {"error": "unknown path"})

    # -- POST -------------------------------------------------------------------
    def _handle_voice(self):
        """Browser-recorded audio -> ffmpeg -> 16kHz WAV -> sherpa-onnx text.

        Audio is deleted right after transcription unless the server runs
        with keep_audio=True; the ledger gets an `asr` event either way."""
        import shutil
        import subprocess
        import tempfile
        from urllib.parse import urlparse, parse_qs

        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send(400, {"error": "empty audio body"})
            return
        if length > MAX_AUDIO_BODY_BYTES:
            # drain the body so the client doesn't die on a broken pipe
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._send(413, {"error": "audio body too large"})
            return
        blob = self.rfile.read(length)
        query = parse_qs(urlparse(self.path).query)
        name = query.get("session", ["sim01"])[0]
        sdir = session_dir(name, self.server.sessions_root)
        if not (sdir / "session.json").is_file():
            self._send(404, {"error": f"session {name!r} not found"})
            return
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self._send(500, {"error": "ffmpeg is required for audio transcoding"})
            return
        import time as _time
        stamp = f"{_time.time():.3f}"
        raw_path = sdir / "requests" / f"voice-{stamp}.webm"
        wav_path = sdir / "requests" / f"voice-{stamp}.wav"
        try:
            raw_path.write_bytes(blob)
            converted = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-y", "-i", str(raw_path),
                 "-ar", "16000", "-ac", "1", "-f", "wav", str(wav_path)],
                capture_output=True, timeout=30)
            if converted.returncode != 0:
                self._send(400, {"error": "audio transcode failed",
                                 "detail": converted.stderr.decode(
                                     "utf-8", errors="replace")[-300:]})
                return
            from .asr import Asr, AsrError
            if self.server.asr is None:
                self.server.asr = Asr()
            try:
                result = self.server.asr.transcribe(wav_path)
            except AsrError as exc:
                self._send(500, {"error": str(exc)})
                return
            if self.server.keep_audio:
                keep_dir = sdir / "audio"
                keep_dir.mkdir(exist_ok=True)
                wav_path.replace(keep_dir / wav_path.name)
                wav_path = keep_dir / wav_path.name
            from .ledger_log import Ledger
            Ledger(self.server.ledger_path).append(
                "asr", session=name, text=result["text"],
                duration_s=result["duration_s"],
                model_sha256=result["model_sha256"],
                audio_kept=self.server.keep_audio,
            )
            self._send(200, result)
        finally:
            raw_path.unlink(missing_ok=True)
            if not self.server.keep_audio:
                wav_path.unlink(missing_ok=True)

    def do_POST(self):
        from urllib.parse import urlparse
        if not self._authorized():
            self._send(403, {"error": "bad session token"})
            return
        if urlparse(self.path).path == "/api/voice":
            self._handle_voice()  # binary body, not JSON
            return
        body = self._read_body()
        if body is None:
            return
        path = urlparse(self.path).path
        if path == "/api/config/kimi-key":
            # 页内配置 Moonshot key: 只进 web 进程内存,不落盘、不入账本、
            # 不进请求文件（与既有安全红线一致）。空串 = 清除。
            key = str(body.get("key", "")).strip()
            self.server.kimi_key = key or None
            self.server.kimi = None  # 下次 use_llm 请求时用新 key 重建
            self._send(200, {
                "configured": bool(key),
                "tail": key[-4:] if key else "",
            })
            return
        name = body.get("session", "sim01")
        sdir = session_dir(name, self.server.sessions_root)
        if not (sdir / "session.json").is_file():
            self._send(404, {"error": f"session {name!r} not found"})
            return
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
        if path == "/api/check":
            text = str(body.get("text", ""))
            if not text.strip():
                self._send(400, {"error": "empty text"})
                return
            normalized = "".join(text.split())
            if normalized in STOP_PHRASES:
                self._send(200, {"handled": "stop_hint",
                                 "note": "停止短语,发送即立即执行"})
                return
            proposal, error, parser_used = self._parse_text(
                text, bool(body.get("use_llm")))
            if error is not None:
                self._send(200, error)
                return
            request_id = _write_request(sdir, {
                "op": "check", "source": parser_used, "raw_text": text,
                "proposal_id": proposal.proposal_id,
                "steps": [step.to_dict() for step in proposal.steps],
            })
            response = _wait_response(sdir, request_id, timeout_s=10.0)
            self._send(200, {"handled": "checked", "parser": parser_used,
                             "matched": [step.to_dict() for step in proposal.steps],
                             "response": response})
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
            proposal, error, parser_used = self._parse_text(
                text, bool(body.get("use_llm")))
            if error is not None:
                self._send(200, error)
                return
            request_id = _write_request(sdir, {
                "op": "sequence", "source": parser_used, "raw_text": text,
                "proposal_id": proposal.proposal_id,
                "steps": [step.to_dict() for step in proposal.steps],
            })
            wait_result = bool(body.get("wait_result", True))
            response = _wait_response(
                sdir, request_id, timeout_s=float(body.get("timeout_s", 120)),
                wait_result=wait_result)
            self._send(200, {"handled": "submitted", "parser": parser_used,
                             "matched": [step.to_dict() for step in proposal.steps],
                             "response": response})
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
    keep_audio: bool = False,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token or secrets.token_urlsafe(24)
    server.registry_path = Path(registry_path)
    server.sessions_root = Path(sessions_root)
    server.ledger_path = Path(
        ledger_path or PROJECT_ROOT / "work/language_control/events.jsonl")
    server.asr = None  # lazily loaded on the first /api/voice request
    server.kimi = None  # lazily constructed on the first use_llm request
    server.kimi_key = None  # in-memory only (set via /api/config/kimi-key)
    server.keep_audio = keep_audio
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    parser.add_argument("--keep-audio", action="store_true",
                        help="keep uploaded voice WAVs under sessions/<name>/audio/ "
                             "(default: deleted right after transcription)")
    args = parser.parse_args(argv)
    server = make_server(args.port, args.registry, args.sessions_root,
                         keep_audio=args.keep_audio)
    url = f"http://127.0.0.1:{args.port}/?token={server.token}"
    print(f"MicroDuck web client (local prototype): {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
