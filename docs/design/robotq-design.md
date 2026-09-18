# robotd 设计文档深度解析：API 层（§3 全章）

> **来源**: https://github.com/pollen-robotics/microduck/blob/main/docs/design/robotd-design.md
> **归档日期**: 2026-09-16
> **归档至**: my-microduck/docs/design/robotq-design.md
> **范围**: 本文档覆盖 robotd-design.md 的 §3（The API）全章——Intents in / State out / Bring-up / Health / Maintenance 命名空间，以及围绕它展开的会话问答（maploc、斜坡实现、Rust 签名、协议解耦、两层权限）。

---

## 目录

1. [API 总览：两种词汇与消息族映射](#1-api-总览两种词汇与消息族映射)
2. [§3.1 Intents in：连续与离散的协议表达](#2-31-intents-in连续与离散的协议表达)
3. [§3.1 坐标系约定：写进协议，删掉一整类 bug](#3-31-坐标系约定写进协议删掉一整类-bug)
4. [§3.1 twist 与 head 分槽：让 last-writer-wins 名副其实](#4-31-twist-与-head-分槽让-last-writer-wins-名副其实)
5. [§3.2 State out：一条流与"报告拒绝"](#5-32-state-out一条流与报告拒绝)
6. [§3.2 握手点名策略与两个易错细节](#6-32-握手点名策略与两个易错细节)
7. [§3.3 Bring-up：enable / init / relax 状态机](#7-33-bring-upenable--init--relax-状态机)
8. [§3.4 Health：什么可以进入判定](#8-34-health什么可以进入判定)
9. [§3.5 Maintenance 独立命名空间](#9-35-maintenance-独立命名空间)
10. [附录 A：会话问答补充](#10-附录-a会话问答补充)

**第二部分：循环周边、存在理由与文档尾章（2026-09-16 续）**

11. [§4.1 循环读快照，永不等](#11-41-循环读快照永不等)
12. [§4.2 Params：配置属于板子](#12-42-params配置属于板子)
13. [§4.3 手柄是客户端（dogfooding 架构学）](#13-43-手柄是客户端dogfooding-架构学)
14. [§4.4 里程计：搭便车的相对运动](#14-44-里程计搭便车的相对运动)
15. [§4.5 四个挂件与设计文档经济学](#15-45-四个挂件与设计文档经济学)
16. [§5 为什么存在：updater 优先、迁移表、两个切片、不回归验收](#16-5-为什么存在updater-优先迁移表两个切片不回归验收)
17. [§6 测试：FakeIo 与失败史驱动的测试清单](#17-6-测试fakeio-与失败史驱动的测试清单)
18. [§7–9 决策台账、推迟清单、开放问题](#18-79-决策台账推迟清单开放问题)
19. [附录：Mapping telemetry（API v24）与多传感器对齐](#19-附录mapping-telemetryapi-v24与多传感器对齐)
20. [附录 A（续）：会话问答 A.6–A.13](#20-附录-a续会话问答-a6a13)

---

## 1. API 总览：两种词汇与消息族映射

robotd 对外 API 被归纳为一对词汇表：**intents in（意图进来）**、**state out（状态出去）**。所有客户端——手柄（padd）、CLI（robotctl）、updaterd、经 mediad 中继的手机/浏览器/LLM——说同一套词汇，所以 mediad 只**中继**帧，不做翻译。

核心洞察：意图天然分两类，而 JSON-RPC 2.0 自带两个消息族，一一对应：

| 意图类型 | JSON-RPC 消息族 | 特征 | 例子 |
|---|---|---|---|
| 连续型 | notification（无 `id`） | 无回复、last-writer-wins | `robot.move`、`robot.head` |
| 离散型 | request（有 `id`） | 必须应答 | `robot.stop`、`robot.enable` |

---

## 2. §3.1 Intents in：连续与离散的协议表达

```jsonc
// 连续：notification，无 id，无回复，last-writer-wins
{"jsonrpc":"2.0","method":"robot.move","params":{"vx":0.2,"vy":0.0,"vyaw":0.4}}
{"jsonrpc":"2.0","method":"robot.head","params":{"neck_pitch":0.35,"head_pitch":0.35,
                                                 "head_yaw":0.0,"head_roll":0.0}}

// 离散：request，有 id，有应答
{"jsonrpc":"2.0","id":7,"method":"robot.stop"}
{"jsonrpc":"2.0","id":8,"method":"robot.enable","params":{"on":true}}
```

**设计 rationale**：

- **50 Hz 下零响应流量**：move/head 是流式的，手柄每 20 ms 就可能发一帧。若每帧都回 ACK，带宽翻倍且毫无意义——反正下一帧马上覆盖上一帧。
- **消息族自动决定 WebRTC 路由**：日后走 WebRTC 时，notification 天然走 unreliable 的 `teleop` 通道，request 天然走 reliable 的 `control` 通道。丢一帧 move 无所谓（下一帧带着最新值马上来），丢一条 enable 必须重传。这正是 architecture.md §5.2 对 robotd 的要求，但它**从消息族自动掉出来**（falling out of the message family），"而不是一条需要任何人记住的规则"。

> **设计哲学**：好的分层让正确的行为成为默认路径，下游需求在上游的抽象里已经隐含。

---

## 3. §3.1 坐标系约定：写进协议，删掉一整类 bug

协议规定：**一切量都是弧度、躯干坐标系（trunk frame）、右手系，符号固定在协议定义里**。

反面教材：旧 runtime 携带五个符号开关——

```
--laser-track-yaw-sign  --laser-track-pitch-sign  --laser-fk-pitch-sign
--laser-fk-neck-sign    --imu-z-rotation-deg
```

存在原因：坐标系约定**从来没被写下来**，每个消费方（激光追踪、前向运动学、IMU）各自在实践中重新发现"这里该取负号"，于是各配一个开关。这是**隐性知识腐化成配置项**的典型。

> **"Writing it into the protocol deletes the category."** 把约定写进协议，不是修了五个 bug，而是删掉了整一类 bug：今后符号错了就是实现错，不存在"我这边的约定"。

---

## 4. §3.1 twist 与 head 分槽：让 last-writer-wins 名副其实

**为什么移动速度（twist）和头部姿态（head）是两个方法而不是一个？**

- **反方案**：合并成一个复合槽位 → 改任一字段需要**读-改-写**整个结构 → 两个客户端并发（手柄驱动身体 + 视觉程序驱动头部）会**静默互相覆盖**，无报错。
- **现方案**：独立槽位 → 每个槽实践上**单写者**（single-writer）→ last-writer-wins 才名副其实。

**每个槽都带时间戳**：控制循环读槽位时问的不是"值是多少"，而是"**这值多老了**"。这正是 deadman 的判据——意图停止到达，速度自动归零；机器人不会拿着 3 秒前的"全速前进"继续走。

**`look`（视线方向）推迟**：两种凝视形式日后都暴露，仲裁规则定为 last-writer-wins 且**不混合**（no blending）——加权平均会产生一个谁也不想要的"折中凝视"。

---

## 5. §3.2 State out：一条流与"报告拒绝"

### 5.1 三个属性

**One stream, subscribable, decimated per subscriber.**

- **一条流**：替换旧 runtime 的六个出口——9870 的 180 字节帧、9871 的 JPEG 流、9872 的 UDP 指令口、9874/9875 的 maploc 端口、web hub 的 `/state.json`。旧世界加一个字段要改四个地方且会**静默不一致**；新世界加一个字段 = 加一个 struct 字段，旧客户端按 JSON 天性忽略不认识的键。
- **订阅制**：`robot.subscribe` 把连接升级为流。默认状态（无订阅者）**什么都不组装**——拼帧要分配内存，控制线程不该无缘无故访问分配器。
- **按订阅者抽稀**：服务端、按连接独立抽稀。10 Hz 仪表盘**真实地**比 50 Hz 数字孪生便宜——在 1GB 内存的 Radxa 上这是实打实的 CPU 与带宽。

### 5.2 核心原则：必须报告被**拒绝**的东西

> It must report what was **refused**, not just what happened.

理由：遥操作 UI 上"摇杆推满、机器人不动、毫无解释"的产品不可用；而 safety 层在**持续不断地**截断指令，这是常态不是边缘情况。

```jsonc
{"method":"robot.state","params":{
  "t":1234.567,
  "move":{"requested":[0.4,0,0],"applied":[0.15,0,0],"limited_by":["max_velocity"]},
  "policy":"walk", "safety":{"fallen":false,"limp":false},
  "loop":{"hz":49.8,"missed":0},
  "battery":{"volts":7.62,"percent":64}
}}
```

逐字段：

- **`move` 三元组是"拒绝上报"的落地**：`requested` vs `applied` 的差值 = safety 干预的痕迹；`limited_by` 用拼写出来的字符串命名原因。
- **`policy`**：回答"这一拍谁在开车"（每拍可变），与握手时的网络文件名（终身不变）是两个问题。
- **`loop.hz: 49.8`**：故意写歪的示例值——提醒这是实测不是理论，健康判定就基于这种数字。
- **`battery` 同时给 volts 和 percent**：映射（6.6V 空 / 8.2V 满负载下 / NP-F550）住在 `duck_control::model::battery_percent`，**算好再发**。旧 runtime 只发电压、App 自己换算，结果同一块电池两个屏幕两个数。"画电池条的客户端不该需要知道机器人配什么电池包。"无库仑计，电压即舵机母线电压，负载下下垂、静止时回弹，percent 会呼吸——这是物理不是 bug。

### 5.3 背压纪律

`robot.subscribe` → 循环往**有界广播**（bounded broadcast）里发，发完就走，**永不等待订阅者**；慢客户端收到**跳帧**（gap）而非背压（backpressure）。这是 invariant #3（控制循环永不等待客户端）在出口方向的落实，也是 updater 进度上报已在用的同款模式。

---

## 6. §3.2 握手点名策略与两个易错细节

### 6.1 SubscribeResult：握手时报网络文件名

`robot.subscribe` 的应答包含：本进程配置了哪些网络（按文件名），以及没人开车时的一句话说明（params 禁用 = healthy vs 加载失败 = unhealthy）。

**为什么放握手而非帧里**：它进程活着就不变；帧上的 `policy` 回答"这一拍谁驱动"是另一个问题；两个 release 装不同步态网络**都报告 `walk`**，"这是哪个网络"只有文件名能回答；放帧里意味着控制线程每拍分配两个字符串去回答一个永不变的答案。

> **原则**：按答案的变化频率分配信道——终身不变的进握手（一次），每拍可变的进帧（50 Hz）。

### 6.2 两个 easy to get wrong 的细节

1. **没人订阅时什么都不组装**——这是机器人的常态。性能要点不是"拼得快"，是"常态下根本不做"。
2. **limit 名为线路拼写出来**，不从 Rust enum 派生——重命名枚举变体不能静默 break 客户端里 `if "max_velocity" in limited_by` 的分支。（深度解读见附录 A.4）

---

## 7. §3.3 Bring-up：enable / init / relax 状态机

### 7.1 核心原则：robotd 绝不自作主张地动

启动时：读当前关节位置 → 采纳为目标 → 不碰力矩。依赖 Dynamixel 硬件特性：**进程死掉期间舵机保持最后收到的目标位置**——所以更新重启无缝，站着的机器人穿过一次更新毫无知觉。

反面方案：启动时插值回默认站姿 → 每次更新重启都驱动站着的机器人（摔倒风险 + 测试 updater 时的混淆变量）。

### 7.2 旧设计的失效方式

旧 `robot.enable` 只翻标志位；力矩藏在 `robotd init` 子命令里——它自己开总线（需先停 daemon）、不见于文档。结果：新机器人按 Start **什么都没发生**——策略在跑、循环在写目标、舵机没力矩全部无视。典型的"各部件各自正常，组合起来静默失效"。

新设计：bring-up 是循环内部的显式状态机，由 `robot.enable` 推进：

```
Limp ──enable(策略已加载 + 有新样本)──▶ Homing(上力矩，2 秒斜坡) ──▶ Ready ──▶ 策略驱动
```

### 7.3 不变量收窄

- **守护的性质不变**："没有任何事因为进程启动而发生"。测试 `a_restart_asks_for_no_torque` 断言的是"**没有任何写发生**"而非"写了一次 false"——"写的缺席"才是对零动作的严格刻画。
- **旧规则错在太宽**："永不碰力矩"比它要守护的性质更宽，代价是每次开车前一个没人记得的手动步骤。新规则：**力矩只随显式 enable 而来**。

> **工程推理示范**：不要捍卫规则的文字，要捍卫规则背后的性质；文字可以重写，性质必须保留。

### 7.4 两个门条件

1. **策略已加载**：enable 语义是"启用策略"；给关节上电去跑一个禁用/加载失败的策略 = 把机器人立在坏 release 上僵住，而 updater 健康门本该拦住它。
2. **有新鲜样本**：2 秒斜坡的起点是关节当前实际位置；从没人读过的位置开始，启动瞬间就是猛冲（lurch）——斜坡存在的全部意义就是消除猛冲。

### 7.5 倒地不是门条件

旧版拒绝倒地 enable，因为当时 `safety.apply` 有 fall gate（倒地摁在 limp 增益），斜坡会写一套"注定不成立的站立"。§2.4 把摔倒判定改成"报告而不 gate"后，该规则的前提消失，随之删除。正面理由：**对躺地的机器人按 Start，恰恰就是"请你站起来"**——斜坡照常跑，站立策略接管。

> **架构演进模式**：删规则靠的不是勇气，是它依赖的前提先消失。

### 7.6 禁用策略不卸力矩

`policy.enabled=false` 后力矩保持，机器人 hold 当前姿势——"a standing robot stays standing"在启动侧与运行侧**两个方向都成立**。

### 7.7 init / relax：直接入口与边沿语义

- "站起来"和"撒手"是独立决策，不该捆绑"启用策略"。旧世界：init 是要停 daemon 的子命令，relax 根本不存在。现在两者都由 robotd 服务，作为**请求**进循环，总线永远只有循环一个写者。
- **`init` 故意不需要策略**：无行走网络的机器人也有权被要求站起来；这也让 bring-up 可测试——CI 没有 ONNX Runtime。（为可测试性设计 API 边界的范例）
- **请求（边沿式）而非标志位（电平式）**：`set_torque` 每关节一次总线事务，电平式会往每拍塞 16 次冗余写。请求每拍取走一次执行完即清空；后到的替换未被读走的先到请求（20 ms 内改主意，第二条才是真意思）。
- **联动**：`relax` 必须顺手清 `enabled`——否则下一拍循环看到 enabled=true 且没力矩，会忠诚地把机器人立刻重新立起来。
- 对照 §3.1：连续 intent 用**电平式槽位**，一次性状态迁移用**边沿式请求**——两种语义两种机制，没有一刀切。

### 7.8 BLE 不可达

init/relax 在蓝牙上不可达：**能把机器人摔在地上的按钮不该出现在远程界面**；站立会同时驱动所有关节，**要求发起者正看着机器人**（物理在场原则）。预告 §3.5。

---

## 8. §3.4 Health：什么可以进入判定

本节地位特殊：§5.1 交代过 robotd 第一个切片不为走路而存在，**是为让 updater 的自动回滚第一次有可信的健康信号**。

### 8.1 四个方法

| method | 回答 | 性质 |
|---|---|---|
| `robot.health` | **循环是否在赶 deadline**（实测频率+错过数）+ 判定从不参考的描述 | 判定+描述打包 |
| `robot.safeToRestart` | 策略启用且机器人在动 → false | 给 updater 的重启许可 |
| `robot.modelApi` | 常数 | 协议版本标识 |
| `robot.remoteSessionActive` | 永远 `false` | 占位——真答案归 mediad；接口完整性优先于单点真实性 |

### 8.2 健康是**发布**出来的，不是**问**出来的

循环每拍更新 atomics（最后拍时间戳+计数器）；IPC 侧收到 `robot.health` 时**自己读原子量**计算答案，**永不问循环**。

反面：问循环"你还好吗"——卡死（wedged）的循环永不回答，health 调用跟着挂，监控系统在最需要答案时拿不到答案。改成读原子量后，**卡死的循环自我暴露**：时间戳停止增长即 unhealthy。证词由嫌疑人的沉默构成。

### 8.3 "60% 的循环"：活着但病了

30 Hz 的循环照样应答每个 RPC、照样写总线——所有 ping 式健康检查都说它健康，但控制质量已烂（策略按 50 Hz 训练）。**这就是控制循环先于一切走路功能被造出来的原因**（§5.1）：健康信号必须先真实，回滚才有意义；旧 runtime 的健康只是"tick 过一次"，所有回滚测试都在对占位符作战。

### 8.4 进入判定的因果标准

> only conditions a *release* can be blamed for may set them

`healthy`/`degraded` 是 updater 的输入（决定回滚），所以判定条件必须满足：**这个毛病换一个 release 能修好吗？**

- 能 → 进判定（策略加载失败、循环不达标——软件的锅）；
- 不能 → 只做**描述**，任何自动决策不许读：电池、电机温度、loop/bus/IMU 计数器。

反例推演：**若电池进判定**——低电量机器人更新后健康门失败 → 回滚 → 用同一块低电量电池审判旧 release → 还失败 → **这台机器从此无法更新**。热下午的电机温度同理。`degraded` 档存在的理由：没上电的台架机器人循环无法达标，但这不是 release 的错。

### 8.5 判定与描述为什么打包在同一个方法

问题的到达方式是一次性的：机器人异常，人问"怎么回事"。只返回判定会立刻引发第二轮提问。而且 loop 小节装的正是判定依据的数字，可直接并置阅读：

```
unhealthy: control loop at 43.9 Hz     ← 判定：不达标
missed = 0                              ← 描述：但一拍没错过
```

并排即区分两种病因：**频率低且 missed=0 = 循环被叫晚**（调度/负载问题）；**频率低且 missed 高 = 一拍内活太多**（代码问题）。两种病因两种修法。

### 8.6 safeToRestart 的物理理由

策略启用且机器人在动 → 不许重启：**迈步途中重启电机控制就是机器人摔倒的方式**。站定时重启无害（§3.3 原地待命接管），跨步单腿承重时就是推倒。

---

## 9. §3.5 Maintenance 独立命名空间

**init、紧急卸力矩、标定、裸关节写不是 intent**——它们是状态迁移/持久配置/safety 不变量的合法例外口，与"每拍持续消费的意图"性质完全不同，所以**另立命名空间**，从协议结构上先分开。

**兑现机制**：mediad 的 relay 按方法名前缀做 per-transport allow-list——teleop 通道放行 `robot.move`/`robot.head`/`robot.subscribe`，拒绝 `maintenance.*`。命名空间分离是 allow-list 能简单工作的前提。

**两层权限的区分**：

- **认证层（signaling gating）**：决定"谁能连上"——回答你是谁；
- **能力层（命名空间+allow-list）**：决定"连上后能调什么"——回答你能做什么。

遥操作员是合法用户，但**合法身份不等于合法能力**。（展开见附录 A.5）

**最重的一句**："`update.*` 到达 DataChannel 意味着远程对等方可以触发回滚。"回滚能换掉整机控制软件——攻击者不需要懂电机、不需要破解协议，只需会发一条 JSON-RPC。防住它不需要密码学，只需要 relay 的名单里**没有这个前缀**。

> 与 §2.4"让错误状态无法表示"同一把刀，这次砍的是攻击面。

---

## 10. 附录 A：会话问答补充

### A.1 maploc 是什么

**问**：what is maploc?
**要点**：maploc = mapping + localization，旧 runtime（apirrone/microduck_runtime）里的自主导航子系统，实际代码在 `apirrone/microduck_maploc_rs`。功能：2D 占据栅格 SLAM（头部 VL53L5CX ToF 测距 + 里程计，Hector 式 scan matching + SE(2) 位姿图回环闭合）、开机蒙特卡洛重定位（MCL）、A* 路径规划输出 vx/wz 给行走策略、TCP 流协议给笔记本查看器——9874/9875 端口由此而来。在 robotd 迁移表（§5.2）中状态为 **unowned**：无新组件接手、无时间表。robotd 只保留 §4.4 的接触式相对里程计（足端锚点+IMU 航向，无漂移校正）。换言之：新架构保留了"本体感觉"（我走了多远），砍掉了"空间自我"（我在地图哪里）。

### A.2 斜坡（ramp）怎么实现

**问**：how to made the 斜坡?
**要点**：斜坡不是 Dynamixel 内置功能，是 robotd 在 50 Hz 循环里手工生成的位置轨迹：`goal(t) = start + (target − start) × (t/T)`，T=2 秒 ÷ 20 ms/拍 = 100 拍，每拍推进一步经 sync_write 下发，宏观平滑 = 100 个微小台阶。两条实现路径：① daemon 内 Homing 状态（非阻塞，拆进 100 个控制拍，health/intent/safety 照常运转）；② standalone init 的 `interpolate_to`（**故意阻塞**的 for+sleep 循环，因为此刻总线本就该只有它一个说话者）。力矩必须先上（`set_torque` 每关节一次事务，这也是 bring-up 是状态机的原因之一）；起点必须是新鲜样本，否则斜坡第 1 拍自己就制造猛冲。runtime 用线性而非 S 曲线——2 秒时长本身就是缓冲，"不要比问题更聪明"。

### A.3 Rust 签名 `fn wire_name(&self) -> &'static str`

**问**：can you introduce this program sentence?
**要点**：逐块拆解——`fn wire_name` 声明函数；`&self` 表示方法且只读借用（对比 `&mut self` 可变、`self` 吃掉所有权），调用走点语法；`&'static str` = 指向生命周期为整个程序运行期的字符串借用，'static 来自字符串字面量编译期烧进二进制只读段。设计动机：① 零分配（对比返回 `String` 每次堆分配，呼应"控制线程不无理由访问分配器"）；② 零开销（match+指针返回）。注意若省略 `'static`，生命周期省略规则会把返回值寿命绑到 `&self` 上——把永久的东西说短了；显式写 `'static` 是声明"这个字符串比任何 Limit 值都活得久"。

### A.4 线上字符串与内部枚举解耦（Hyrum's Law）

**问**：how to understand "线上的字符串是手写固定的协议常量，与代码内部命名解耦"?
**要点**：serde derive 让枚举变体名同时承担两个身份——内部标识符（随时重构，编译器保证同步）与线上协议常量（受众是看不见的所有客户端，改了静默破坏兼容）。共享一个字符串的后果：重命名 `MaxVelocity→VelocityLimit` 编译通过、测试通过，客户端 `limited_by` 分支从此静默失效。解法：`wire_name()` 返回手写固定字符串，改枚举名=内部重构（线上不动），改字符串=协议变更（显式编辑+review 显眼+golden test 守住）。根源：编译器的能力边界是仓库边界，协议受众在边界之外。更大的原则即 Hyrum's Law 的防御：凡可能被依赖的可观察行为，要么写进显式契约并冻结，要么从可观察面删掉——**自由和承诺不该共用一个标识符**。同构案例：§3.1 符号开关收敛进协议、§3.2 电池换算收敛到机器人侧、§2.5 JOINT_NAMES 用 const 断言锁死。

### A.5 两层权限：信令门控 vs 能力层

**问**：how to "Signaling gating 决定谁能连上；它不说一个遥操作员同时也是修理工"?
**要点**：区分认证（authentication）与授权（authorization）。第一层信令门控：WebRTC 握手期验证凭证、建立加密通道，粒度只到"这个连接合法吗"，不区分遥控/维修/换软件。第二层能力层：mediad relay 按"身份×通道"过滤方法名前缀，同一身份走 teleop 通道只能 move/head，走任何远程通道都发不出 maintenance.*/update.*。必须两层的原因：① 握手期不存在"方法调用"粒度，能力判断天然属于消息流经处；② 能力是"身份×通道"的二维函数，信令层只验身份表达不了；③ 纵深防御——两层独立失效才出事，每层假设上一层可能失守。类比酒店：前台（信令）决定你能进楼，每把门锁（allow-list）决定你进哪个房间——酒店不会靠"在前台拦住非维修工"来保护配电室。

---

## 贯穿全章的设计哲学（总结）

1. **让正确行为成为默认路径**：消息族选对，WebRTC 路由就不用记规则；槽位拆对，并发冲突从结构上消失。
2. **让错误状态无法表示**：safety 独占 RobotIo 写柄（§2.4）、维护操作从远程可达面删除（§3.5）、重启零写入由测试断言（§3.3）。
3. **知识收敛到事实所在的一端**：电池换算在机器人、抽稀在服务端、坐标约定在协议——凡"每个消费者各自推导"的东西最终都会分叉。
4. **判定必须附带证据**：state 帧的 requested/applied/limited_by、health 的判定+描述同包——系统不仅要说出结论，还要交出得出结论的证据。
5. **按变化频率分配信道**：终身不变进握手，每拍可变进帧，环境量只做描述不进判定。
6. **捍卫性质而非规则文字**："永不碰力矩"收窄为"力矩只随显式 enable 而来"；删规则的前提是它依赖的旧前提先消失。

---

# 第二部分：循环周边、存在理由与文档尾章

## 11. §4.1 循环读快照，永不等

标题即纪律：**The loop reads snapshots, never waits**。四条规则：intents/params 由 IPC 线程发布、循环单次原子读；无任何东西可对循环施加背压；无请求同步进入循环；遥测走有界广播，慢订阅者收 gap。

### 11.1 两个世界之间的四道海关

图的左右两半是**两个运行时、两种时间观**：左边 tokio 多线程 IPC（"事件何时来"），右边独立 runtime 的 50 Hz 控制线程（"每 20ms 一拍"）。四个交接结构按数据语义各选机制：

**① intent slots（`ArcSwap`，带时间戳）——电平式**。写者构造新快照一次原子换入，读者一次原子 load 拿走完整快照：无锁、无等待、无半写状态。循环看到的是"此刻最新的意图"，不是"自上次以来的所有事"。

**② power req / skill flags（taken once per tick）——边沿式**。动词从 load 变 **taken**：每拍取走并清空，一次性状态迁移执行完即消失。

**③ atomics（publish）——循环 → IPC**。循环每拍顺手更新计数器，IPC 侧随时自读；循环永远不知道有没有人在问它健不健康。

**④ broadcast（bounded, drop-on-lag）——状态帧出口**。有订阅才拼帧；通道满了慢订阅者丢帧，循环发完即走。

### 11.2 画眼：没有反向通道

> No channel runs the other way.

四个结构全部单向。没有任何"IPC 发问 → 循环回答"的同步调用——同步请求-响应意味着调用方要等被调方，而"等"在控制循环的字典里不存在。卡死的循环拖不住 health 调用，暴走的客户端拖不住循环——**结构上就没有"等"的边**。这是 invariant #3/#4 在数据结构层的最终形态。

### 11.3 skill flags 为什么是布尔数组

- 队列（反 A）：一拍内连按两次踢球会攒两脚连踢——20ms 格子里第二次按下同键**不携带额外信息**；
- 单 last-writer 槽（反 B）：一拍内先踢球再坐下，踢球请求**人间蒸发**——两个不同请求都该被看到；
- 布尔数组（现）：同键连按幂等无累积，不同键各置各位一拍内全可见。恰好卡在队列与单槽之间：**"同时发生"在 20ms 格子里是真实的，"先后发生"在这个格子里是幻觉**。

---

## 12. §4.2 Params：配置属于板子

### 12.1 基本形态

TOML 文件，启动读、**大部分不监听**（not watched），住在 `releases/<ver>/` 之外（`/etc/robot/robotd.toml`）——所以在更新**和回滚**中幸存。选址即声明：配置属于板子，不属于 release。

### 12.2 两个热更新例外，各被重启代价挣来

- **padd 监听 `[pad]` / `[pad_imu_head_control]`**（每秒 stat，mtime 变才重读）：按键映射从手机改，重启 padd 会断会话 → deadman 把行走中的机器人急停；
- **robotd 被请求时重读 `[policy]`**（mode/enabled 除外）：`robotctl policy add` 落地新技能而不夺走站着机器人的电机控制。

**明确不做**：运行中重读 `[safety]`/`[control]`——安全与控制参数热更新是大得多的承诺。机制兜底：`robotctl configure` 知道每个键要哪种生效方式，说不清的键让 robotctl 测试失败。

### 12.3 属于板子的双面性

- 默认策略路径指向 release 内部：更新把策略和为它训练的二进制放一起；手改路径在板级配置里**粘住**（survive update）；删掉覆写即回默认。开发与量产各得其所。
- 文件可完全缺席：裸板用内建默认值启动，比"拒绝启动的 daemon"远程易诊断。
- **kP 120/160 事故**（learned the hard way）：某人取消注释写上 120（当时是默认值）→ release 默认前进到 160 → 该板被覆写冻结 → 扩散到全机队。无报错、无日志，只是"软了"。**取消注释 = 亲手把那台板子从 release 时间线上摘下来**。此事故被写进 robotd.toml 文件头作永久警示。

### 12.4 数量级与预设

**约 10 个值，不是 142 个**：旧 runtime 参数爆炸大多是变体/死技能/死传感器——砍硬件变体的直接红利。`policy.mode`（walk/roller）是唯一的粗粒度开关：一次选定加载哪些策略 + 整套调参默认，未设字段按 mode 解析。**是预设不是变体**：代码路径一条，数据两组。

---

## 13. §4.3 手柄是客户端（dogfooding 架构学）

### 13.1 padd 的构成与独立理由

`padd` = 读 `gilrs` → 把摇杆变 intent → 经 socket 发给 robotd。独立 crate 的理由：让手柄栈（gilrs → libudev C 依赖）远离 robotctl——**恢复工具的依赖树越薄越可靠**，调试工具的纯洁性优先于代码紧凑。

### 13.2 一次 socket 跳转买来什么

成本几十微秒。收益：**App、SDK、远程客户端用的输入通路，就是开发者天天用手柄踩的那条**——"so it cannot quietly rot"。反面是特权通道：手柄走内部通道天天被用，公共 API 只在发布前被测，用户的痛苦你最后知道。

> **文化层面的 dogfooding 是"请大家多用自家产品"；架构层面的 dogfooding 是"让开发者别无选择，只能用用户的那条路"。**

开发红利：`ssh -L /tmp/robotd.sock:/run/robotd.sock` 一条命令实现手柄在笔记本、机器人在桌上——socket 天然可转发，内部函数调用则不可能。

### 13.3 无特权是承重墙

`padd.service` 开机跑、无手柄时无害（不发 + deadman）、**unprivileged**（只有 `input` 和 `robot` 组）。两个推论：

1. **配对被赶出 padd**：蓝牙绑定要 root + BlueZ，配对归 configd——"要了 root 就不再是客户端"是逻辑推论不是品味判断；
2. 持特权的 padd **不再是 App API 的真实用户**——它会忍不住走后门。保持 padd 手无寸铁 = 保持它和 App 一样无能 = 它替所有未来客户端踩出障碍。

---

## 14. §4.4 里程计：搭便车的相对运动

**输入**：循环本拍已读的关节位置 + IMU 四元数（零额外总线事务）；**输出**：`odom: {position, yaw}` 上 state 流；**消费方**：`robotctl monitor` 的路径图。

**Contact-based 原理**：选一个脚底角作地面锚点 → 经 kinematics FK 反推躯干世界位姿 → 另一只脚的角降得更低时锚点接力（迈步不跳变）；航向 = IMU 积分 yaw，世界系 = 开机朝向。

**诚实边界**：无磁力计、无漂移纠正——**这是相对运动，每个消费方都必须这样对待它**。

**成本与翻案**：每拍两次链求值 + 零总线流量，跑在循环 50 Hz 而非原型机的独立 100 Hz。§7 原本有 "no odometry" 决策，被**翻案（reversed）而非重辩（re-argued）**：当初反对的理由从来不是算力，是"没人读答案"；monitor 路径图出现后，前提变化 → 结论机械跟随。

---

## 15. §4.5 四个挂件与设计文档经济学

slice 2 后长出的四个非控制非安全子系统，共享形状：挂在 tick 上、**绝不阻塞 tick**、碰总线必须经循环仲裁的 intent。

| 模块 | 干什么 | 关键设计点 |
|---|---|---|
| `sound.rs` | 发声，单 `aplay` 子进程，新声杀旧声 | codec PCM 独占——"新杀旧"是硬件事实的直接表达 |
| `theremin.rs` | ToF 深度(15Hz)→音符+张嘴+状态行 | 被 50Hz 循环采样但**永不等待** |
| `chorale.rs` | 鸭子合唱：最小 id 当指挥，指挥管排位，btd 只搬信标不思考 | 选举=比 id，通信层保持愚蠢（详见 A.8） |
| `pet-detect/` | ~20KB CNN 跑麦克风 log-mel，独立 worker | 模型小到荒谬，循环外运行 |

外加 `soc.rs`：sysfs 读板温，**故意不在 RobotIo 后**——总线死掉时它必须还能回答（总线全灭 vs 主板过热是同一症状，直到两个数都看见）。

**核心论点**：没有设计文档是**规则在起作用而非缺陷**。规则：一个服务挣得设计页的时机 = "第二个读者将不得不从代码反推它的契约"。这四个模块实现唯一、消费者唯一、决策局部——归宿是模块头注释（给读代码的人）+ cheatsheet（给操作的人）。**文档不是勋章，是协调成本的预付**；给"何时写文档"立显式规则，和给"何时热加载""何时进判定"立规则是同一种心智。

---

## 16. §5 为什么存在：updater 优先、迁移表、两个切片、不回归验收

### 16.1 §5.1 目标不是"好的 robotd"

两个目标：①控制核心快速迭代；②**真正测试 updater**——是第二个重排了工作顺序。更新引擎写完但从未上真机：`systemctl restart` 未遇过真 systemd；30s 健康门超时是承认的猜测；最糟——**自动回滚只有在 robot.health 真的意味着什么时才成立**，而旧定义是"循环 tick 过一次"，此前每次回滚测试都在对占位符作战。所以第一个切片不学走路：**它存在是为了在真板上做一份诚实的健康信号**。

### 16.2 §5.2 迁移表：分诊而非大爆炸

旧 runtime 五项工作五种命运：控制循环→robotd（done）；手柄→padd（done）；相机/检测/JPEG→mediad（M5）；web hub/PWA/brain 口→mediad/App（M5+）；**maploc→unowned（—）**。过渡期两者 side by side 但**绝不同时**（一条总线一个主人，systemd `Conflicts=` 机制强制）。remote/app 层是 reachy_mini 架构的 Rust 移植，out of scope，对 robotd 的唯一要求 = §3.5 命名空间隔离。

### 16.3 §5.3 两个切片

**Slice 1 — hold the pose**：tick/总线/模型/RobotIo/诚实健康，**什么计算都没有**——故意。无智能则任何异常不可能是算法 bug；总线负载与 50Hz 是真的则时序健康是诚实的；坏 release 砸上去机器人不会摔（本来就站着），可以在台架上砸一整天 install/rollback/power-cut。Done when 四条：①定姿一小时零总线错误；②update apply 装/重启/过门/**机器人没动**；③**故意不健康的 release 被自动回滚**；④**更新中途断电**经 boot counter 恢复。

**Slice 2 — walk and stand**：observation/ONNX/safety/intents/手柄。Done when：真板上走且**经 intent API 驱动**；更新重启干净过门；`--unhealthy` **仍然**回滚（"still"——每层新能力到货后安全性质必须重新验证而非继承假设）。

> Everything since arrived **on top of that shape** rather than changing it —— 好架构的标志：后来的需求以"添加"而非"修改"落地。

### 16.4 §5.4 不回归即验收

`bench_dynamixel_bus` 报告达成频率/抖动/读时/总线时/占用/错误/IMU 新鲜度（50 与 100Hz）：记下今天的数字作基线，robotd 必须打平。**明确拒绝 RT 三板斧**：无 `SCHED_FIFO`、无 pinning、无 `mlockall`——旧 runtime 普通调度已够稳，为不存在的问题付复杂度税不值。目标函数：**约束=循环可靠性≥基线；优化=周围代码变简单**。

---

## 17. §6 测试：FakeIo 与失败史驱动的测试清单

`FakeIo`（脚本化样本、可冻结可故障）让 `cargo test` 无硬件无网络无 Docker——trait 边界的红利：控制逻辑与硬件时序变成两个可分别测试的东西。

六条测试 = 六块失败史的碑：①deadline 错过→health false；②启动采纳当前姿势**永不命令运动**；③safety 拒 NaN/截超程，**摔倒不抢占策略不改增益**；④limp-fall 摔倒触发、落脚与静态倾斜不触发，斜坡终点是站姿；⑤intent 停止→deadman 速度归零；⑥**golden observation vectors**——(mjlab 输入, 期望 61 维数组) 对，从训练侧导出提交比对。

金样本防的是**静默语义错误**：索引错位不崩溃、测试绿、网络照样输出——产出"一个貌似合理但会摔倒的机器人"。没有它只能真机摔鸭子数天；有了它是 CI 里一次 diff。

> Each test's comment says which failure it exists to prevent —— 注释必须写明防哪个失败：测试意图可审计、防凑覆盖率、测试清单即系统"已知死法大全"。

---

## 18. §7–9 决策台账、推迟清单、开放问题

### 18.1 §7 Decisions recorded（13 条，四家族）

- **边界由编译器守**：duck-control 为 workspace crate（无第二仓库）；手柄独立 crate（gilrs 远离恢复 CLI）。
- **继承什么重写什么**：总线层新写常数借用（代码薄、调好的数字不重推导）；优先级链保持 runtime 形状（技能按其怪癖调过）；Rust 常量建模（世界上只有一台机器人）。
- **运行时行为**：IMU 进电机的 sync_read（同总线设备，无 IMU 抽象）；启动采纳当前姿势；bring-up 状态机非标志位；摔倒判定只报告不 gate。
- **配置与验证**：参数文件不监听；策略路径默认=release 目录；仿真排 slice 2 后（硬件是验证路径，FakeIo 覆盖笔记本）。

最珍贵一行是**带删除线的翻案**：`~~no odometry~~ — reversed` 保留原文+删除线+翻案理由——决策记录在乎的不是对了多少次，而是**每次转向是否可追溯**；它防轮回、示范翻案正当程序、传递"承认旧决策不丢人"的文化信号。

### 18.2 §8 Deferred, deliberately

十项主动推迟：MuJoCo 后端与 RemoteIo、skill 抽象、policy bundle manifests 与 model_api gating、look/pose/do intents、gaze IK、live params reload 与 config store、thermal limits、rate limits、per-device IMU 标定。副词 deliberately 是关键：**被看见、被命名、被推迟**，而非失控积压。每项推迟时写下理由 = 未来翻案的判据（里程计即凭此毕业："nothing read it"→"monitor now reads it"）。

### 18.3 §9 Open（五道未解题，各附已知线索）

1. **Radxa 上的控制频率**：50Hz 是 Pi Zero 2W 遗产，板子有了可测——答案在仪器不在辩论；
2. **金样本地位降级**：从"真相之源"降为"训练环境的回归检查"，非任何已交付功能的前置；
3. **关节级限位不存在**：safety 只截到执行器行程，拦得住 NaN 拦不住机械不明智位姿；真限位在未搬入的 alpha MJCF。**不随手抄的原因**："一个像解剖限位但不准的限位，会暗示没人拥有的保护"——宁可明示保护不存在，不提供假保护；
4. **alpha MJCF 住哪**（31KB XML + 19MB mesh 还在旧 runtime）；
5. **C 依赖的经常性成本**：gilrs 无条件拉 libudev-sys，板上路径优先纯 Rust；附 *Unverified on macOS* 标注（aarch64 sysroot 缺，变通 `-p updater -p robotd -p robotctl` 或 Linux 构建）——连"未验证"都显式标注。

三张清单构成完整认知状态：§7 已决定（含翻案史）、§8 决定暂不做（带复活条款）、§9 还不知道（带全部线索）。**没有知识黑洞**。

---

## 19. 附录：Mapping telemetry（API v24）与多传感器对齐

### 19.1 需求与三项增量

远端建图器（视频流另一端：今天笔记本，日后服务器）需要三样 `robot.state` 没有的：与 `tof.frame` 共享的时钟、原始 IMU（非 projected gravity 加工品）、相机/ToF 在哪。全部 additive——旧客户端无感。

### 19.2 三项内容

- **`t_ns`**：CLOCK_MONOTONIC 纳秒（`proto::clock`）。`t`/`at_us` 保留（单流读者要从 0 开始的数）。`media.video` 应答在**同一瞬间**读 `mono_ns`+`real_ns`——RTP 时间戳（RTCP 以墙上钟陈述）由此换算进单调钟轴；
- **`imu: {gyro, quat}`**：躯干 IMU 原料，50Hz，nothing above it。头 IMU 归属 tofd，被读时傍 `tof.frame` 流出——**数据跟着产生它的 daemon 走**；
- **`frames: {camera, tof}`**：本拍 **measured** 头关节经 `kinematics::head::HeadFk` 的躯干系位姿——与 `robot.look` 同一 FK（永不分叉）；静态几何走 `robot.model` 一次性问答——**a client asks rather than transcribes**。

### 19.3 成本与位置

三个小 struct/发布拍、仅订阅时、FK ~50ns（20ms 预算的十万分之二）。maploc 被 §5.2 判 unowned 后，建图以新形态回归：**鸭子当诚实的传感器平台（测得准、时间真、位姿实），算力活交给视频那端**——1GB 内存约束下的正确分工。（时钟与对齐机制的展开见 A.12、A.13。）

---

## 20. 附录 A（续）：会话问答 A.6–A.13

### A.6 TOML 是什么，robotd.toml 干什么

**问**：what is toml, and what is robotd.toml use for?
**要点**：TOML = Tom's Obvious, Minimal Language，为"人类手改配置"而生的极简格式：节 `[section]`、键值、注释 `#`、数组；注释掉的键=**不存在**（程序用内建默认）。对比 JSON 无注释、YAML 缩进陷阱。robotd.toml 是**每台鸭子唯一的板级参数表**（`/etc/robot/robotd.toml`），三重身份：① 板子与 release 的分界线（住 releases 外、幸存更新与回滚、安装器永不覆盖、可整个删除）；② "谁读它"的地图——四进程共用一文件（`[media]`/`[duck_detector]`→mediad，`[head_imu]`→tofd，`[pad_imu_head_control]`→padd，其余→robotd），改 `[media]` 重启的是 mediad；③ 默认值文档——注释写明每个数字来历（50Hz 未在 Radxa 重新推导、stall_periods 从 3 放宽到 25 因调度抖动误杀好 release、鸭子检测 threshold 是 INT8 量化下的是/否闸）。文件头永久引用 kP 120/160 事故。

### A.7 kP 是什么，120 与 160 的事故

**问**：what is kp120 and 160?
**要点**：kP = 舵机内部位置环 PID 的**比例增益**：力矩指令 ∝ kP×(目标−当前)。大→硬/快/可能抖，小→软/抗扰差。它是 release 级调参结论（策略按特定 kP 训练）。事故：某人在板级 TOML 取消注释写 120（当时=默认）→ release 默认前进到 160 → 覆写冻结该板 → 扩散全机队。无报错无日志，只是"软了"——**配置漂移不产生故障，只产生无法解释的差异**。与 A.6 的"取消注释=摘下时间线"互为因果。

### A.8 chorale.rs 鸭子合唱团

**问**：what is chorale.rs?
**要点**：多鸭合唱同一首曲子。核心难题=**无共享时钟的多机合奏**，解法=指挥鸭的节拍计数器即时间基准（对钟问题转化为"听指挥"）；指挥=最小 id；声部按各鸭音域（per-robot personality）分；`btd` 只搬信标不思考；`robotctl chorale` 启动寻伴。默认 `accept=false` 因为**合唱会驱动电机**（嘴和头）——陌生鸭子走进房间就自顾自摇头是"没人要求的运动"；且关闭=**射频完全隐身**（不发任何信标）——"礼貌拒绝"本身也是存在性泄露。架构地位：§4.5 挂件之一，发声走 sound.rs、运动走循环仲裁 intent，无后门。

### A.9 回滚与回滚机制

**问**：what is 回滚, what is 回滚机制?
**要点**：回滚=撤销更新回到上一已知正常版本。机器人场景必须全自动（用户家里没人按 reset）：A/B 双分区保留旧版 → 切新版重启 → **30s 健康门**反复问 robot.health → 过门转正/不过自动回切 → 断电也算失败（boot counter）。机制的每一步都机械可靠，**唯一判断环节是"新版健康吗"**——健康信号假则"机制全对、判断全错"（有保险但验货员是瞎子）。故 §5.1 把 updater 测试排在走路之前；§3.4 的因果纯度（低电量不该触发回滚）、§3.3 的原地待命（回滚=计划外重启不许摔倒）、§4.2 配置住 release 外（回滚不冲配置）都是为它服务。

### A.10 bench_dynamixel_bus

**问**：introduce the bench_dynamixel_bus
**要点**：总线压测工具（源码在原型 runtime，文档两处权威记载）。以指定频率对总线反复执行控制循环的读写，不控制行为只测"这条路本身多快多稳"。七指标：achieved rate / jitter / read time / bus time / utilisation / errors / IMU sample freshness。三用途：① 重写验收基线（§5.4 新旧之争=两组数字对比）；② 重新验证 50Hz 继承值（§9.1，也许 Radxa 能更快）；③ 硬件排障第一站（社区复刻实证：第三方 TTL 转接板错误计数非零=方向切换跟不上 1Mbps）。镜像：robotd 循环自带五分钟汇总报同一组数——bench 是诊所体检，汇总是随身自检。

### A.11 workspace crate 与 mod 的区别

**问**：how to understand "模块之间是 crate 与 crate 的关系"
**要点**：mod 之间 `use crate::xxx` 随手就来；crate 之间必须先在自己 Cargo.toml **显式声明依赖**。五个实质差别：① 依赖显式且 `cargo tree` 可查；② `pub` 出 crate 才是公开 API（第二道闸门）；③ **传递性切断**——duck-control 的 Cargo.toml 没有 tokio，则 `tokio::` 三字在其宇宙中不存在（编译报 unresolved import）；④ 独立编译单元，变化传染切断；⑤ **循环依赖物理禁止**，依赖图天然 DAG，"底层知道上层"写不出来。类比：mod=同楼各部门门禁通用，crate=独立法人子公司各有账本。`duck-control as a workspace crate` 把"控制 vs 守护进程"这条最重要的缝从人肉约定升级为编译器机制。

### A.12 CLOCK_MONOTONIC

**问**：how to understand CLOCK_MONOTONIC
**要点**：Linux 两类钟：`CLOCK_REALTIME`=挂钟（现实时间，**会被 NTP/管理员/闰秒调整跳变**）；`CLOCK_MONOTONIC`=秒表（开机至今，**严格单调永不可调**）。融合算术的核心运算是减法（帧间隔），挂钟跳变一次 dt 就错几秒、点云错位——故遥测轴必须单调。视频是异类（RTP 时钟，RTCP 以墙上钟陈述），翻译靠 mediad **同一瞬间**读 (mono_ns, real_ns) 锚点对做坐标平移；"同一瞬间"三字重要——两读数隔 1ms 词典就固有 1ms 误差。细节：MONOTONIC 休眠时不走（BOOTTIME 才计入，鸭子休眠即无帧可对故自洽）；纳秒是容器选择（64bit 到 2554 年不溢出）非真实分辨率。纪律：**所有跨流运算只许在单调钟轴上做**。

### A.13 IMU / ToF / Camera / 状态四流如何对齐

**问**：so IMU, TOF, Camera, micro-duck states how to align together?
**要点**：分两个独立问题。**时间对齐**：tof.frame 与 robot.state 天然同轴（同板同单调钟 t_ns），只有视频是异类——RTCP 给每帧 RTP 序号 X 配挂钟时刻 Y（帧的"挂钟出生证"），经锚点词典平移 `mono = Y − real_ns + mono_ns` 得秒表时刻；之后四流同轴，低频消费者向高频原料就近取样（state 50Hz vs tof 15Hz vs 视频 30fps，错位上限 ±10ms≈2mm）。**空间对齐**：一切归 trunk frame 再抬世界——ToF 像素 × robot.model 波束方向 = 传感器系点云；× frames.tof（**measured** 关节 → HeadFk 真实位姿）= 躯干系点云；× imu.quat + odom = 世界系（开机朝向，相对运动）。四道防掺假：measured 非指令角；look 与建图同一 FK；静态几何问而不抄；IMU 给原料非加工品。重活全在远端，鸭子只出"测得准、时间真、位姿实"——**对齐问题在协议设计那天就被预解了**（整机一时钟域、全协议一坐标系）。
