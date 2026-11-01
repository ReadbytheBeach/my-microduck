# 语言控制 MicroDuck：方案流程图

> 日期：2026-11-01。四张流程图 + 关键设计点，拆解"从语言/文字到最终结果"的完整链路。
> 配套：原理介绍 `语言控制MicroDuck原理介绍.md`、网关细则 `网关的规则.md`、
> 操作手册 `语言控制MicroDuck方案操作手册.md`、Web 使用 `语言控制MicroDuckWeb操作指南.md`。

## 图 1 · 全景：一句话的完整旅程

```mermaid
flowchart TD
    A["👤 你: 打字 或 说话"] --> B{输入方式}
    B -- "文字" --> C[输入框]
    B -- "语音" --> D["🎙 浏览器录音<br/>MediaRecorder"]
    D --> E["上传 web 服务<br/>ffmpeg 转 16kHz WAV"]
    E --> F["sherpa-onnx 离线转写<br/>不出本机"]
    F --> G["回显: 我听到的是……<br/>文本填入输入框"]
    G --> C
    C --> H["⏱ 停顿 0.5s 自动预检 /api/check"]
    H --> I{解析: 规则<br/>或勾选了 Kimi}
    I -- "规则解析器" --> J["查说法表+正则<br/>快/免费/离线"]
    I -- "Kimi 接地匹配" --> K["对照注册表理解<br/>自由表述/多步/参数"]
    J --> L{解析成功?}
    K --> L
    L -- "失败" --> M["你是不是想说: ……<br/>建议层,不执行"]
    L -- "成功" --> N["提案: 技能+参数<br/>如 walk vx=0.15 ×2s"]
    N --> O["会话服务静态校验<br/>只查不执行 op=check"]
    O --> P["页面显示:<br/>网关校验 ✓/✗ + 匹配结果"]
    P --> Q{"👤 你点 3.发送<br/>(语音/文字'停下'除外→立即停)"}
    Q --> R[正式递交 /api/say]
```

## 图 2 · 点发送之后：五道关卡

```mermaid
flowchart TD
    R["/api/say 正式递交"] --> S1{"① 是'停下'类短语?"}
    S1 -- 是 --> Z["立即拉闸<br/>不解析不排队不等网络"]
    S1 -- 否 --> S2["② 解析成提案<br/>(发送前已预检过,这里复跑同一逻辑)"]
    S2 --> S3{"③ 网关判决<br/>gateway.py 174行纯代码"}
    S3 -- "结构不符" --> X1["红 policy_rejected"]
    S3 -- "技能没登记" --> X2["灰 capability_gap"]
    S3 -- "哈希不符/超限" --> X3["红 policy_rejected"]
    S3 -- "状态不允许" --> X4["红/黄 需显式确认"]
    S3 -- "全过" --> S4["④ 会话服务排队<br/>sessions/name/requests/"]
    S4 --> S5["⑤ 逐步执行<br/>上一步 ACK+结果到手<br/>才发下一步"]
    S5 --> W["写指令文件 command.json<br/>command_id防重放<br/>lease租约防失控"]
```

## 图 3 · 仿真侧时序：50Hz 的握手（以"向前走两秒"为例）

```mermaid
sequenceDiagram
    participant S as 会话服务<br/>session_service.py
    participant F as command.json<br/>(指令文件)
    participant I as Isaac 播放进程<br/>50Hz 控制循环
    participant L as 哈希链账本

    S->>F: 写入指令 {command_id, vx:0.15, 2s, 租约3s}
    Note over S,L: 账本记 command 事件
    loop 每个控制步 (1/50秒)
        I->>F: 读指令,校验 session_id/序号
        I->>I: 应用速度→策略网络→14关节
        I-->>S: 回写 ACK "第4步真正用上了"
    end
    Note over S,I: 服务看到 ACK 才算数
    I->>I: 走满100步(=2.0s,按步数不按墙钟)
    I-->>S: 结果 completed, sim_steps=100
    S->>S: 结果到手→才发序列的下一步
    S->>L: 账本记 step_completed
    I->>I: 相机帧→ffmpeg→clips/0001_xx.mp4<br/>+ latest.jpg 预览帧
```

## 图 4 · 结果怎么回到你眼里

```mermaid
flowchart LR
    J["执行结果<br/>completed/failed+原因"] --> K["动作结果面板<br/>完整JSON"]
    J --> M["历史面板<br/>原文→提案→网关→结果"]
    J --> N["账本 events.jsonl<br/>哈希链,可审计防篡改"]
    O["视频片段 clips/NNNN_xx.mp4<br/>每条命令一段"] --> P["仿真画面面板<br/>点击即播"]
    Q["latest.jpg 每秒一帧"] --> P
    N -. "断链即拒出报告" .-> R[实验治理]
```

## 关键设计点（容易忽略的几件事）

1. **语音和文字在进入解析前就已经合体**——ASR 只负责把声音变文字，之后走完全相同的流水线，不存在"语音专用通道"，少一条路径少一类 bug
2. **预检和正式发送用的是同一段校验代码**（`_validate_steps`）——你看到的 ✓/✗ 和发送后的判决不会分叉
3. **Kimi 只在第②步**——它的工作到"产出提案"为止；第③步网关是纯代码，不认识 Kimi，也不给 Kimi 面子
4. **"发了指令"≠"执行了"**——必须收到播放进程的 ACK 才认账；租约到期没续约，鸭子自动停
5. **两秒是数出来的，不是等出来的**——按 50Hz 控制步数（2s=100步），不用墙钟也不用浮点仿真时间（那个浮点误差 bug 真实咬过人）
6. **失败会截断序列**——"走→转身→站住"如果转身那步被拒，"站住"不会执行，不会留下不可预测的状态
7. **每条命令一段视频**——你想核对"刚才那步走成什么样"，不用拖整段录像，直接点对应片段
