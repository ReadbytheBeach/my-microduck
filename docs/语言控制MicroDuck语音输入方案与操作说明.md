# 语言控制 MicroDuck：语音输入方案与操作说明（已实施）

> 日期：2026-10-30 定稿，2026-10-31 实施完成。
> 原定决策：USB 麦克风插 DGX Spark 本机；ASR 引擎 sherpa-onnx 本地离线（aarch64）。
> **路线修正（2026-10-31）**：收音改在浏览器侧（MediaRecorder，127.0.0.1 属 secure
> context 可直接 getUserMedia），音频上传本机服务器转写——不再依赖服务器端声卡/
> sounddevice，麦克风由浏览器选择。ASR 仍是 sherpa-onnx 本地离线。
> 环境核查（2026-10-30）：本机 aarch64 GB10，无采集设备（arecord -l 无 capture 项），
> hf-mirror 可用。实施结果（2026-10-31）：sherpa-onnx 2.0.1 aarch64 wheel 可用，
> paraformer-zh 模型经 hf-mirror 逐文件下载成功（`hf download` CLI 报 "Local entry
> not found"，改用 API 列文件 + curl resolve 直下）。

## 一、方案概要

```
浏览器 MediaRecorder(USB 麦,webm/opus) → 上传本机 web 服务 → ffmpeg 转 WAV 16kHz 单声道
      → sherpa-onnx(paraformer-zh) → 转写文本
      → 回显"我听到的是：……" + /api/check 实时可执行性(绿/黄/红) → 用户点发送
      → 现有 SequenceParser → 提案 → 网关 → 会话服务
```

要点：语音转写后就是"文字输入"，**复用已验收的 M0–M2 全链路，不新增第二条执行路径**。停止指令（"停下"）转写命中即本地立即执行，不等任何确认。

安全确认机制（已实现形态）：

- 语音一律先回显转写文本 + 自动 /api/check 校验，**用户点"发送"才执行**（比原定分级更保守，全部指令都要确认）；
- "停下"等停止短语：/api/check 返回 stop_hint，前端立即走 /api/stop，不等确认；
- 空转写或与句式差距过大：/api/check 判红，提示重说，不猜。

账本新增 `asr` 事件（会话名、音频时长、转写文本、模型 SHA-256、是否留存）；音频默认不保留（web 服务 `--keep-audio` 除外，存 `sessions/<name>/audio/`）。

会话录像（已实现）：`mdctl session start --backend isaac --video` → 整段 `sessions/<name>/session.mp4` + 每命令片段 `sessions/<name>/clips/NNNN_<command_id>.mp4`（短于 12 帧的闭环 supersede 碎片自动删除）+ 1Hz 预览帧 `latest.jpg`。Web 页面"仿真画面"面板 1Hz 刷预览图，片段点击即播。默认关（录像有 GPU 开销）。

## 二、实施清单（已完成，见第四节）

依赖、模型、代码、测试均已于 2026-10-31 完成；过程记录与坑见第四节。sherpa-onnx aarch64 可用，未启用 faster-whisper 降级。

## 三、操作步骤

> 更完整的页面区域说明与常见问题见《语言控制MicroDuckWeb操作指南.md》。

```bash
cd ~/microduck-ros2-isaac
PY=work/dance/microduck_rl/.venv/bin/python

# 1. 起 Isaac 会话（带录像）
$PY -m language_control.mdctl session start --backend isaac --name voice01 --video

# 2. 起 Web（终端打印带 token 的 URL，浏览器打开）
$PY -m language_control.web --port 8787

# 3. 浏览器里：
#    - 文字：输入框打字 → 500ms 后自动显示可执行性(绿/黄/红) → 点"发送"
#    - 语音：点"🎙 语音"（首次浏览器弹麦克风权限）→ 说话 → 点"■ 停止录音"
#      → 回显"我听到的是：……" + 自动校验 → 确认无误点"发送"
#    - "停下"：语音/文字命中立即停止，不等确认
#    - 视频：画面面板 1Hz 预览 + 每条命令一个 mp4 片段，点击播放

# 4. 清理
$PY -m language_control.mdctl session stop --name voice01
```

预期注意：麦克风权限走浏览器（与系统 arecord 无关）；转写错误被"回显确认 + /api/check + 网关"三层兜底，误执行不会发生。

## 四、实施过程记录

（实施时逐条追加：日期 / 改动文件 / 验证命令 / 实际结果 / 坑）

- 2026-10-30：方案定稿（本文档）。环境核查：本机 aarch64 GB10，当前无采集设备，无 ASR 包，hf-mirror 可用。用户决策：USB 麦 + sherpa-onnx。
- 2026-10-31：实施完成（M2 Web v2 增强）。
  - 改动文件：`language_control/asr.py`（新）、`language_control/web.py`（/api/check、/api/voice、/api/clips、/api/clip、/api/frame.jpg + 前端实时校验/录音/视频面板）、`language_control/session_service.py`（op=check 只校验不执行、--video 透传）、`language_control/mdctl.py`（session start --video）、`language_control/ledger_log.py`（asr 事件类型）、`scripts/session_clip_recorder.py`（新，每命令片段）、`scripts/play_isaac_velocity.py`（会话录像片段/预览帧/hold 早退收尾修复）。
  - 依赖：`uv pip install --python work/dance/microduck_rl/.venv/bin/python sherpa-onnx`（2.0.1，aarch64 wheel 可用，未降级 faster-whisper）。
  - 模型：`work/language_control/asr_model/`（model.int8.onnx 232MB + tokens.txt，SHA-256 加载时计算并入账本）。**坑**：`hf download` CLI 对 hf-mirror 报 "Local entry not found"，改用 API 列 siblings + curl resolve 逐文件下载。
  - 验证命令：`pytest tests/ --ignore=tests/test_dance_slope_terrain.py`（226 passed + 2 env-gated skip + 85 subtests，既有 Isaac import error 除外）；ASR 冒烟 `test_wavs/0.wav` 转写正确。
  - 用法变化：语音在 Web 页面上（🎙 按钮，浏览器弹麦克风权限），转写回显后自动跑 /api/check 给绿/黄/红，确认后点发送；"停下"转写命中立即执行。不再有 `mdctl say --voice`。
  - 会话录像：`mdctl session start --backend isaac --video` → session.mp4 + clips/ 每命令片段 + latest.jpg 预览帧（1Hz），Web 页面直接看。

## 五、明确不做

不接真机；不做唤醒词/声纹/连续监听；音频默认不保留；不改 M0 固定命令路径；不训练；Web 端不做 Kimi LLM 解析（仍走本地规则解析器）。
