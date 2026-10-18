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

## 方案一句话

4 块 12×12 压阻电子皮肤（头部左右 + 两足底）→ STM32G030 扫描前端 → STM32G431 `skin_hub` 以 Dynamixel 从机 **ID 201** 挂舵机总线；软件侧新增 `tactiled` 独立守护进程（不碰总线，JSON-RPC 订阅 `robotd`），两阶段落地：先 I2C Stemma 零侵入验证，再并入总线进 50Hz 控制环。
