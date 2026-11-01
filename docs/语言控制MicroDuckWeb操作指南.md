# MicroDuck 语言控制 Web v2 · 完整操作步骤与解释

> 日期：2026-11-01。适用版本：M2 Web v2（实时校验 + 语音 + 视频节选）。
> 前置：`mdctl session start` 与 web 服务都用解释器 `work/dance/microduck_rl/.venv/bin/python`。

## 一、启动（两个终端窗口）

**终端 1 — 起仿真会话（鸭子本体）：**

```bash
cd ~/microduck-ros2-isaac
PY=work/dance/microduck_rl/.venv/bin/python
$PY -m language_control.mdctl session start --backend isaac --name voice01 --video
```

- `--backend isaac`：真实物理仿真（首次启动要 2~4 分钟编译加载，耐心等）
- `--name voice01`：会话名，自己起
- `--video`：**要画面必须加**。开启整段录像 + 每命令片段 + 预览帧（有 GPU 开销，默认关）
- 看到 `"state": "running"` 即就绪。注意 `--steps` 别传小值（那是会话总仿真预算，默认 200 万步够用很久）

**终端 2 — 起 Web 服务：**

```bash
cd ~/microduck-ros2-isaac
work/dance/microduck_rl/.venv/bin/python -m language_control.web --port 8787
```

终端会打印一行带 token 的 URL，浏览器打开它：

```
MicroDuck web client (local prototype): http://127.0.0.1:8787/?token=xxxx
```

页面打开后**自动连上最新的运行中会话**（就是 voice01），顶部"会话"栏会显示名字和状态。想换会话：输入名字点"切换"，不用改 URL。

## 二、页面各区域怎么用

### ① 提案/网关（输入区）

- **打字**：输入"向前走两秒"，停半秒，"可执行性"自动出结果：
  - 🟢 绿色 ✓ 可执行（n步）→ 点"发送"就执行
  - 🔴 红色 ✗ + 原因（比如"翻跟头"是 experimental 被拒、"蹲下"是 pending）→ 发了也会被网关拦，不用试
  - 灰色 → 会话没起来
- **语音**：点"🎙 语音"（第一次浏览器会弹麦克风权限，允许）→ 说话 → 点"■ 停止录音" → 页面回显"我听到的是：……"并自动校验 → **确认转写无误后点"发送"才执行**
- **"停下"**：不管打字还是语音，命中立即停止，不等任何确认——这是安全后门
- 转写错了怎么办：直接在输入框里改文字再发，或者说"停下"重来

### ② 执行进度 + 停止按钮

- 实时显示仿真状态（当前速度、位置、步数）
- 红色"■ 停止"随时可按，独立通道，不被任何东西阻塞

### ③ 仿真画面（--video 启动才有）

- 上方预览图：约 1 秒刷一帧，看鸭子"正在动"
- 下方片段列表：**每条命令一个 mp4**（文件名 `0001_xxxxxxxx.mp4` 按命令顺序编号），点击即播。刚执行完的命令几秒后出现。面板顶部显示片段所在目录；同名会话重新 `session start` 会清空上一轮的 session.mp4/预览帧/片段，**列表里永远是本次会话的片段**（要留存请先复制走）
- 想复盘"刚才那步走成什么样"→ 点最新片段；整段录像在 `work/language_control/sessions/voice01/session.mp4`

### ④ 动作结果

最后一条命令的完整响应（completed/failed + 原因）

### ⑤ 技能面板

每个技能的 usage/evidence 状态和当前会话可执行性（✓/✗），绿色才能跑

### ⑥ 历史

最近的原文→提案→网关判定流水

## 三、推荐玩法顺序

1. 先打字发一条"向前走两秒"，看预览图里鸭子走 + 片段列表出现 0001 片段
2. 再试语音说同样的话，体验"回显→确认→执行"
3. 试一句会被拒的（"给我跳个舞"），看红色拦截提示——网关在工作
4. 走的过程中按一次"■ 停止"，鸭子应立即站住
5. 试闭环："向前走30厘米"（走准了自动停，终点误差厘米级）

## 四、结束

```bash
$PY -m language_control.mdctl session stop --name voice01   # 终端1
# 终端2 直接 Ctrl-C
```

## 五、背后发生了什么（一图流）

```
你(打字/语音) → 浏览器
  语音: MediaRecorder 录音 → 上传 → sherpa-onnx 本地离线转写(不出本机)
  文字/转写 → /api/check 只校验不执行(解析+注册表+限值+闭环全查一遍) → 绿/红提示
  点发送 → SequenceParser 规则解析 → 提案 → 网关白名单 → 会话服务
    → 命令文件 → Isaac 播放进程(鸭子动) → ACK/结果回传
  同时: 播放进程 camera → ffmpeg → session.mp4 + 每命令片段 + 预览帧 → 页面
安全兜底: 转写回显确认 + check 预检 + 网关拦截 + 停止独立通道,四层
```

## 六、常见问题

- 页面灰/报 session not found → 会话没起或已退出，终端 1 重新 start（web 不用重启）
- 无预览帧 → 会话没加 `--video`
- 语音按钮报错 → 浏览器没给麦克风权限，地址栏左侧小锁图标里改
- 鸭子站定后新指令不走 → 这是已知的 velstand 策略层"站立不动吸引子"，停掉会话重开即可（已立项待处理，不是 Web 的问题）
- 想保留语音音频 → web 启动加 `--keep-audio`（存 `sessions/<name>/audio/`，账本记留存标记）
