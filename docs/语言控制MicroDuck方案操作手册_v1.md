# 语言控制 MicroDuck 方案操作手册（M0）

> 版本：M0（2026-10-26）。范围：文字输入（无 ASR）、仿真回放（无真机通路）、单技能单次独立回放、不训练。
> 本手册与代码一一对应：`language_control/` 包 + `scripts/build_skill_registry.py` + `tests/test_language_control.py`。

## 三条红线（评审三原则，全方案最高约束）

1. **策略形状兼容 ≠ 控制语义兼容**：obs[61]/action[14] 形状正确、文件哈希正确，都不构成"兼容"或"验收通过"的证据。注册表契约逐字段标注来源；任何字段缺失受控来源即标 `unverified`；生成器（`scripts/build_skill_registry.py`）禁止从"哈希+形状"推断可用性。
2. **技能导入 ≠ 技能获准调用**：使用状态三级（`registered` 仅展示/dry-run / `experimental` 须显式 `--experimental` / `available` 指定模型+适配器+场景内可调用）与证据状态（`passed`/`failed`/`pending`/`unknown`）完全分离。证据只来自独立验收记录（`language_control/acceptance_records.json`），没有记录就是 `unknown`，永远不可能凭空变成 `passed`。
3. **dry-run 成功 ≠ 仿真动作成功**：M0-A（离线测试）与 M0-B（真实仿真回放）两级验收分别陈述结论；每次执行的结果分列三判断——命令构造是否正确 / 仿真进程是否正常完成 / 动作目标是否有证据（无评价器时明示"未验证"；退出码 0 但结果文件缺失或不匹配绝不报成功）。

## M0 全流程图

```mermaid
flowchart TD
    A[终端输入文字] --> B{本地拦截: 停止/停下/停?}
    B -- 是 --> C[executor.cancel<br/>不经过 Kimi]
    B -- 否 --> D{解析器}
    D -- 默认 --> E[规则解析器<br/>有限语法·完整匹配]
    D -- --llm --> F[Kimi 客户端<br/>strict JSON Schema,<br/>不支持则降级普通 JSON]
    E --> G{匹配?}
    E -- 否定/条件句 --> X1[parse_error 拒绝]
    E -- 距离/时长<br/>通用"跳舞" --> X2[capability_gap 拒绝]
    E -- 先…再… --> X3[policy_rejected<br/>多步骤整体拒绝]
    F -- HTTP/超时 --> X4[provider_error]
    F -- JSON 非法 --> X1
    G -- 否 --> X2
    G -- 是 --> H[proposal<br/>len steps==1 · 60s 有效期 · target=sim]
    H --> I{gateway 网关}
    I -- 过期/真机/多步 --> X3
    I -- 不在白名单 --> X2
    I -- 哈希不匹配 --> X3
    I -- 限值按编码检查<br/>velocity 限值 / phase 无速度参 / posture_flag 不适用速度限值 / constant 无命令字段 --> I
    I -- registered+execute --> X3
    I -- registered+execute+--research --> J[研究回放,标签入帐]
    I -- experimental 无 --experimental --> X3
    I -- experimental+failed 无双标志 --> X3
    I -- available 场景内 --> K[adapter 构造启动模板]
    J --> K
    K --> L{execute?}
    L -- 否 dry-run --> M[只构造不启动<br/>结论: 命令构造已验证]
    L -- 是 --> N[Popen 非阻塞<br/>最小 env, 独立进程组]
    N --> O{启动标记 180s?}
    O -- 超时 --> P1[failed: startup timeout]
    O -- 到达 --> Q[running · 后台监控]
    Q --> R{取消?}
    R -- 停止 --> S[cancel_requested<br/>killpg INT→TERM→KILL<br/>确认进程组清理] --> T[cancelled]
    R -- 否 --> U{进程结束}
    U -- exit≠0/超时 --> P2[failed]
    U -- exit=0 --> V{结果文件核验<br/>steps/checkpoint/command/trace}
    V -- 缺失/不匹配 --> P3[failed: 退出0但证据缺失/不符]
    V -- 一致 --> W[done: 完成了一次独立回放]
    X1 & X2 & X3 & X4 --> LGR[(链式账本<br/>work/language_control/events.jsonl)]
    C & M & P1 & P2 & P3 & T & W --> LGR
```

## 架构与代码对应

| 模块 | 文件 | 职责 |
|---|---|---|
| 意图解析 | `language_control/intent.py` | 规则解析器（完整匹配有限语法，非关键词提取）；Kimi 客户端（urllib、OpenAI 兼容端点、strict JSON Schema、不支持自动降级普通 JSON） |
| 提案 | `language_control/proposal.py` | Proposal dataclass；M0 校验 `len(steps)==1`（多步骤整体拒绝）；60s 有效期；统一错误分类 parse_error/provider_error/capability_gap/policy_rejected/execution_failed |
| 网关 | `language_control/gateway.py` | 白名单、按编码类型分组的限值、使用状态×证据状态检查、提案有效期、真机目标一律拒绝 |
| 适配器 | `language_control/adapters.py` | 受控启动模板（解释器/argv/cwd/最小 env/超时/期望输出），LLM 不得输出路径/任务名/shell 片段；isaac_playback（四种编码映射）+ mujoco_baseline（仅 velocity） |
| 执行器 | `language_control/executor.py` | subprocess.Popen 非阻塞；idle→starting→running→done/failed；取消 cancel_requested→cancelled（确认进程组清理后才进 cancelled）；三类时间分离 |
| 账本 | `language_control/ledger_log.py` | 复用 `scripts/foot_structure_ledger.py` 链式账本；事件类型 intent/proposal/gate/execution/cancel/capability-gap；密钥与认证头不落账 |
| CLI | `language_control/cli.py` | 终端循环，执行在后台线程，"停止"即时可达；三判断分列反馈 |
| 注册表生成 | `scripts/build_skill_registry.py` | 组合 upstream lock/manifest + 本地技能配置 + 独立验收记录；只组合不推断 |

## 注册表字段（work/language_control/skill_registry.json）

每个技能一条记录：

- `model`：路径、期望 SHA-256、哈希来源（upstream.lock / 本地 artifact 出处）、实测哈希、`hash_status`（ok/mismatch/missing）；
- `kind`（生命周期维度）：perpetual / scripted / episodic / builtin；
- `command.encoding`（输入生成维度，与 kind 独立）：velocity / constant / phase / posture_flag / builtin；
- `contract`：六个契约字段（obs 定义 / 归一化 / 命令编码 / 动作定义 / 时序 / 运行环境），每字段 `{value, source, status}`，status 只有 verified（有受控来源）或 unverified（待核验）；
- `usage_state` × `evidence_state`：分离的两个维度；
- `limits`：仅 velocity 编码有限值（vx ±0.4 / vy ±0.3 / yaw ±1.0，来源 velocity_env_cfg.py:113-116）；posture_flag 不适用速度限值；
- 编码规则：phase 的执行时长 = `period_s × end_phase`（steps = round(period_s×end_phase×50Hz)），禁止把 duration_s 传给 `--phase-clock`；constant 编码命令槽钉零。

生成器一致性规则：`usage_state=available` 必须同时满足 evidence=passed、contract 全字段 verified、hash ok，否则降级为 registered 并输出 WARNING——升级只能靠追加验收记录，不能靠改配置硬提。

## 配置与 CLI 用法

```bash
cd ~/microduck-ros2-isaac
PY=work/dance/microduck_rl/.venv/bin/python

# 重建注册表（改配置/追加验收记录后）
$PY scripts/build_skill_registry.py

# 交互模式（默认 dry-run：只构造命令不启动）
$PY -m language_control.cli

# 执行一条（registered 技能需 --research 研究回放名义；experimental 需 --experimental）
$PY -m language_control.cli --once "向前走"                       # dry-run
$PY -m language_control.cli --once "向前走" --execute --research --wait
$PY -m language_control.cli --once "跳侧步舞" --execute --experimental --research --wait

# 停止：交互中输入"停止"（本地即时取消）；或 Ctrl-C 退出
# 校验账本链
$PY scripts/foot_structure_ledger.py verify work/language_control/events.jsonl
```

白名单语法（完整匹配）：走/前进/向后走/向左走/向右走/左转/右转/原地走、站立/站好/别动、坐下/站起来、翻跟头、左踢/右踢、蹲下、捡东西、跳侧步舞（须显式指名，"跳个舞"不自动绑定）、停止。否定句、条件句、"走三十厘米"类距离/时长目标、多步骤指令一律拒绝并分类入账。`--llm` 走 Kimi 端点（需 `KIMI_LC_API_KEY`，可选 `KIMI_LC_BASE_URL`/`KIMI_LC_MODEL`）；密钥不进代码/账本/git/子进程环境。

## 验收结论

### M0-A（离线测试）——通过

`work/dance/microduck_rl/.venv/bin/python -m unittest discover -s tests`：**141 例，139 通过 + 1 跳过 + 1 个既有 import error**（`test_dance_slope_terrain` 的 `microduck_isaac_lab` 模块缺失是既有问题，非本次回归；基线 93 例同样表现）。其中 `tests/test_language_control.py` 48 例，覆盖评审最小测试集全部条目：

- 注册表三方一致性（lock/manifest/验收记录；11 技能、12 模型文件、哈希全 ok）；
- 哈希不匹配拒绝启动（构造方与网关两侧）；
- 61D/14D 形状 + 哈希正确但编码未核验 → 不自动启用（demote 测试）；
- 三类编码限值互不通用（velocity 越界拒绝；posture_flag 不接受速度参数；phase 不接受速度参数；constant 拒绝命令字段）；
- 语义反例："不要翻跟头"不执行（parse_error）；条件句"如果有人挡路就停下"明确拒绝；"走三十厘米"/"走两分钟"不改写成速度/时长任务（capability_gap）；"先蹲下再站起来"多步骤整体拒绝（policy_rejected）；"跳个舞"不自动绑定 dance_side_step；
- pending/failed 普通模式拒绝、`--research` 显式放行且标签入账；experimental+failed 须双标志；
- 替身进程：启动中与运行中取消（请求可达、进程组确认清理后才 cancelled）；重复提交不重复启动；
- 退出码 0 但结果文件缺失/不匹配 → 不报成功（三判断：process_completed=True 但 action_evidence=missing/mismatch，总结论 failed）；
- API 失败/输出非法/未知技能分别记录 provider_error/parse_error/capability_gap；
- 真机目标拒绝；账本链完整 + 篡改可检测 + 密钥脱敏。

**M0-A 通过后仅宣称**：文字到结构化提案、权限检查和命令构造已验证。

### M0-B（真实 Isaac 仿真回放）——三条编码家族各一条，全部通过

执行环境：Isaac Sim 6.0.1 / Isaac Lab 3.0.0-beta2（upstream.lock validated_toolchain），任务 `Isaac-MicroDuck-Velocity-Flat-Play-v0`，机器人 `robot_allcollisions.usda`，seed 42，num-envs 1，headless。每条均通过完整语言链（文字→提案→网关→适配器→执行器），非直接调脚本。

| 编码 | 指令 | 实际命令（适配器构造） | 命令构造 | 进程完成 | 动作证据 |
|---|---|---|---|---|---|
| velocity（walk） | 向前走 | `isaaclab.sh -p scripts/play_isaac_velocity.py --device cuda:0 --policy-onnx reference/policies/v7/velstand.onnx --num-envs 1 --seed 42 --command-vx 0.15 --command-vy 0.0 --command-yaw 0.0 --steps 100 --output … --trace …` | ✓ | ✓ exit 0 | passed（输出 steps/checkpoint/command 与期望一致；实测 mean_planar_speed 0.134 m/s vs 指令 0.15，前向位移 0.172 m/2s，done_count=0） |
| phase（ground_pick） | 捡东西 | 同上但 `--policy-onnx …/alpha_ground_pick.onnx --phase-clock 4.0 --steps 140`（=4.0s×0.7×50Hz，duration_s 未传给时钟） | ✓ | ✓ exit 0 | passed（steps/checkpoint 一致；trace 140 行、相位 0.0→0.695 完整扫到 end_phase；done_count=0，根高度 0.065→0.117 m） |
| posture_flag（sitdown，研究回放，标签保留） | 坐下 | 同上但 `--policy-onnx …/alpha_sitstand.onnx --command-vx 1.0 --command-vy 0.0 --command-yaw 0.0 --steps 100` | ✓ | ✓ exit 0 | 进程级 passed（command [1,0,0] 确认送达）；**动作语义未验证**：done_count=1、min root height 0.005 m，无评价器区分"受控坐下"与摔倒，ramp/unwind 固定标志复现未核验 → evidence 保持 pending |

三条回放目录：`work/language_control/executions/ccbba025…`（walk）、`9e9f87b7…`（ground_pick）、`bed740ad…`（sitdown）；账本 22 事件链 `foot_structure_ledger.py verify` 通过。

**M0-B 通过后仅宣称**：walk（velocity）、ground_pick（phase）、sitdown（posture_flag，研究回放标签保留）三个技能在"指定模型 + isaac_playback 适配器 + 上述平地配置"下已通过语言适配链启动并产生可核验结果。不宣称跨地形鲁棒性，不宣称真机安全性。每次回放独立初始化，不暗示跨进程连续动作。

## 技能落位表（M0-B 后）

| 技能 | 模型 | kind | 编码 | usage | evidence | 备注 |
|---|---|---|---|---|---|---|
| walk | v7 velstand.onnx | perpetual | velocity | **available**（限 M0-B scope） | **passed** | M0-B 后由生成器核验提升 |
| stand | v7 alpha_stand.onnx | perpetual | velocity(zero) | registered | unknown | 编码仅本地声明，契约 partial |
| sitdown | v7 alpha_sitstand.onnx | scripted | posture_flag(sit=1.0) | registered | pending | 研究回放已跑（标签保留），动作语义未验证 |
| standup | 同上 | scripted | posture_flag(stand=0.0) | registered | pending | 同上 |
| roulade | v7 roulade.onnx | episodic 1.0s | constant | registered | unknown | |
| kick_left | v7 ball_kick_left.onnx | episodic 0.5s | constant | registered | unknown | |
| kick_right | v7 ball_kick_right.onnx | episodic 0.5s | constant | registered | unknown | |
| crouch | v7 roller_crouch.onnx | episodic 3.5s | phase(5.0×0.7) | registered | unknown | manifest 声明 action_scale=0.8/roller 模式，本地回放接口不支持 → 已登记未启用 |
| ground_pick | v7 alpha_ground_pick.onnx | episodic 2.8s | phase(4.0×0.7) | **available**（限 M0-B scope） | **passed** | M0-B 后提升 |
| dance_side_step | 本地 V4 dance_policy_v4_hard_floor.onnx | perpetual | phase(4.9667×1.0) | experimental | failed | 九组工程回放完成但位移/跨材质鲁棒性未通过；单种子、无真机；"跳个舞"不自动绑定 |
| stop | 无（网关内置） | builtin | — | available | unknown | 本地直接取消，不经过 Kimi |

模型文件数 12（v7 十个 ONNX + 本地 dance V4 一个 + legacy playground sitstand 一个单独登记）／技能数 11／启用（available）技能数 3（walk、ground_pick、stop）——三者分列，不混。

## 已声明的接口缺口（不硬凑）

- crouch/roller 系：manifest 声明 `action_scale=0.8`，`play_isaac_velocity.py` 无 action-scale 参数 → crouch 保持 registered+unknown；
- constant 编码（roulade/kick）：上游 manifest 无 command 块，编码为本地声明 → 契约 partial，不启用；
- mujoco_baseline 适配器仅支持 velocity 编码（脚本接口如此），M0 只做 dry-run 构造验证；
- Kimi 端点 strict JSON Schema 支持未实测（本机无可用于探测的独立 key，harness 凭据不外用）→ 客户端已实现 strict 优先 + 400 自动降级普通 JSON，网关独立校验不依赖模型自律；默认路径为规则解析器。

## M1–M3 展望

- **M1**：多技能序列（解除 len(steps)==1 限制）、仿真内受控停止、运行时指令注入（改回放脚本）、距离/时长目标的受控映射、Kimi 端点接入实测与严格 schema 核验；
- **M2**：动作评价器接入（trace 指标自动判定动作目标，替换"进程级证据"）、dance_side_step 位移/鲁棒性整改后重验、sitstand ramp/unwind 固定标志复现核验、constant 编码家族 M0-B 核验；
- **M3**：真机通路（独立评审后）、ASR 输入、多种子统计验收。

## 明确不做（M0 边界复核）

不碰真机与 ros2_ws；无 ASR；不改 `play_isaac_velocity.py`；不训练；不改 v7 manifest 与 upstream.lock；密钥不进代码/账本/git/子进程环境；多技能序列与仿真内受控停止留 M1；不 git commit。
