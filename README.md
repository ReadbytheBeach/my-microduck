# my-microduck

Pollen Robotics **MicroDuck**（$399 开源 RL 双足鸭子机器人）的电子皮肤研究、商业分析与视频驱动舞蹈强化学习实验归档。

## 内容

- `report/` —— 终版技术方案（Markdown + 4 张技术插图）
  - [MicroDuck电子皮肤部署方案_终版_全机12x12.md](report/MicroDuck电子皮肤部署方案_终版_全机12x12.md)
  - 插图：总体架构拓扑 / 电路框图 / 软件架构 / 机械安装
- `sessions/` —— 讨论存档（技术论证过程 + 商业分析 + 工程答疑）
  - [Session 01 · MicroDuck 电子皮肤部署与商业分析](sessions/01-MicroDuck电子皮肤部署与商业分析.md)
  - [Session 02 · MicroDuck 视频驱动舞蹈 V1–V4 两日进展](sessions/02-MicroDuck视频驱动舞蹈V1-V4进展.md) —— 视频重定向、mjlab 训练、ONNX 与 Isaac 多地形/多材质验收
- `guides/` —— 实操手册
  - [microduck 安装手册（DGX Spark / aarch64 实装记录）](guides/microduck安装手册.md) —— Isaac Sim 6.0.1 + Isaac Lab v3.0.0-beta2 + torch cu130 在 GB10 上的完整安装与验证流程
  - **语言控制 MicroDuck 系列（M0–M2 + Web v2，2026-10/11）**
  - [语言控制MicroDuck原理介绍](guides/语言控制MicroDuck原理介绍.md) —— 四道关卡架构（解析→提案→网关→会话）+ 4 张配图（[images/](guides/images/)）
  - [语言控制MicroDuck的方案流程图](guides/语言控制MicroDuck的方案流程图.md) —— 四张 mermaid 流程图：全景旅程 / 五道关卡 / 50Hz 握手时序 / 结果回路
  - [语言控制MicroDuckWeb操作指南](guides/语言控制MicroDuckWeb操作指南.md) —— Web 页面完整操作：文字/语音输入、Kimi 技能匹配、实时校验、视频节选
  - [语言控制MicroDuck语音输入方案与操作说明](guides/语言控制MicroDuck语音输入方案与操作说明.md) —— sherpa-onnx 离线 ASR 方案与实施记录
  - [语言控制MicroDuck方案操作手册](guides/语言控制MicroDuck方案操作手册.md) —— M0–M2 实现全记录、技能落位终表、Web v2 使用规则

## 方案一句话

4 块 12×12 压阻电子皮肤（头部左右 + 两足底）→ STM32G030 扫描前端 → STM32G431 `skin_hub` 以 Dynamixel 从机 **ID 201** 挂舵机总线；软件侧新增 `tactiled` 独立守护进程（不碰总线，JSON-RPC 订阅 `robotd`），两阶段落地：先 I2C Stemma 零侵入验证，再并入总线进 50Hz 控制环。
