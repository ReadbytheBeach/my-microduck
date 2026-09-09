# microduck 安装手册（DGX Spark / aarch64 实装记录）

> 本文档是 2026-09-09 在 NVIDIA DGX Spark（GB10 Grace Blackwell，ARM64）上
> 完成 microduck-ros2-isaac 项目 **Isaac 路线** 的完整安装与验证记录。
> 上游文档：https://osrbot.github.io/microduck-ros2-isaac/zh/guide/installation
> （上游测试环境为 Ubuntu 24.04 x86_64，本手册为 aarch64 适配版。）

## 1. 最终环境一览

| 组件 | 版本 | 位置 |
|---|---|---|
| 操作系统 | Ubuntu 24.04.4 LTS (aarch64) | — |
| GPU 驱动 | 580.142 | 系统 |
| Isaac Sim | 6.0.1（6.0.1-rc.7+release.42383）standalone | `~/isaacsim-6.0.1` |
| Isaac Lab | v3.0.0-beta2（commit 28a37ce） | `~/rlgpu_ws/IsaacLab` |
| PyTorch | 2.10.0+cu130（CUDA 13.0，GPU 可用） | Isaac Sim 自带 Python 环境 |
| ONNX Runtime | 1.24.4（aarch64，CPU-only） | `~/microduck-ros2-isaac/work/isaac_python_pkgs` |
| microduck 仓库 | commit a2e7f32（2026-09-08） | `~/microduck-ros2-isaac` |

关键软链：`~/rlgpu_ws/IsaacLab/_isaac_sim -> ~/isaacsim-6.0.1`
（microduck 脚本通过 `$ISAACLAB_DIR/_isaac_sim` 定位 Isaac Sim，此链接不可缺。）

## 2. 与既有环境的隔离说明

机器上原有 Isaac Sim 5.1.0-rc.19 源码构建与 IsaacLab main 分支，位于
`~/Desktop/xj_code/`。本次安装**完全并行、未触碰**旧环境：

- 新版 Isaac Sim 6.0.1 解压到独立目录 `~/isaacsim-6.0.1`；
- 新版 Isaac Lab 克隆到默认路径 `~/rlgpu_ws/IsaacLab`（microduck 脚本默认找这里，
  无需设置 `ISAACLAB_DIR`）；
- microduck 的准备脚本只写项目内目录（`reference/`、`work/`），
  对 IsaacLab checkout 只读不写。

两者唯一共享的是 Omniverse 缓存（`~/.cache/ov` 等，按 Kit 版本隔离，互不破坏）
和显存（不要同时跑两个仿真即可）。

## 3. 安装步骤（可复现）

### 3.1 前提检查

```bash
nvidia-smi        # 驱动 580.142，满足 ≥580.95.05；注意避开 595.71.05（6.0.1 在其上会 GPU 崩溃）
ldd --version     # GLIBC 2.35+（Ubuntu 24.04 满足）
```

### 3.2 安装 Isaac Sim 6.0.1 aarch64 standalone

```bash
cd ~/Downloads
wget -c "https://downloads.isaacsim.nvidia.com/isaac-sim-standalone-6.0.1-linux-aarch64.zip"   # 约 10.5 GB，免登录直链
mkdir ~/isaacsim-6.0.1
unzip -q isaac-sim-standalone-6.0.1-linux-aarch64.zip -d ~/isaacsim-6.0.1
cd ~/isaacsim-6.0.1 && ./post_install.sh
```

### 3.3 安装 Isaac Lab v3.0.0-beta2

```bash
mkdir -p ~/rlgpu_ws && cd ~/rlgpu_ws
git clone https://github.com/isaac-sim/IsaacLab.git
cd IsaacLab && git checkout v3.0.0-beta2
ln -sfn ~/isaacsim-6.0.1 _isaac_sim
./isaaclab.sh -i
```

验证 PyTorch 为 cu13 构建（GB10 强制 CUDA ≥ 13）：

```bash
conda deactivate   # 重要：先退出 conda，否则报 "SRE module mismatch"
./isaaclab.sh -p -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
# 期望输出：2.10.0+cu130 13.0 True
# 若显示 cu12x 或 False，重装 cu130：
./isaaclab.sh -p -m pip install -U torch torchvision --index-url https://download.pytorch.org/whl/cu130
```

### 3.4 安装 microduck 项目依赖

```bash
git clone https://github.com/osrbot/microduck-ros2-isaac.git ~/microduck-ros2-isaac
cd ~/microduck-ros2-isaac
./scripts/fetch_upstream.sh          # 拉取上游策略/资产到项目内 reference/
./scripts/setup_isaac_python_env.sh  # uv pip install --target work/isaac_python_pkgs（项目本地，--no-deps）
```

说明：onnxruntime 1.24.4 有 aarch64 wheel，脚本无需修改；但 Linux aarch64 的
onnxruntime 是 CPU-only，策略推理跑在 CPU 上，对单个 ONNX 策略足够。

## 4. 验证结果（本机实测）

1. **torch**：`2.10.0+cu130 13.0 True` ✓
2. **Isaac Lab 启动**：`scripts/tutorials/00_sim/create_empty.py --headless`
   应用加载并进入仿真循环 ✓（该脚本为无限循环，确认启动后手动停止）
3. **microduck playground 冒烟**：

   ```bash
   cd ~/microduck-ros2-isaac
   ./scripts/run_isaac_playground.sh --headless
   ```

   实测结果：7 个 ONNX 策略全部加载（walking / standing / sitstand /
   ground_pick / kick_left / kick_right / roulade），PhysX 后端注册成功，
   MicroDuck 14 关节全部初始化，GPU 利用率约 44%，无错误。✓

唯一警告（无害）：onnxruntime CPU 版尝试发现 GPU 设备失败
（`device_discovery.cc ... /sys/class/drm/card0/device/vendor`），
aarch64 CPU-only 预期行为，可忽略。

## 5. 日常使用

```bash
# 注意：若 shell 自动激活了 conda base，先 conda deactivate，
# 否则 isaaclab.sh 会报 "AssertionError: SRE module mismatch"

# GUI 玩法（DGX Spark 接本地显示器，不支持 Livestream）：
cd ~/microduck-ros2-isaac
./scripts/run_isaac_playground.sh --follow-camera --viz kit

# 无头冒烟：
./scripts/run_isaac_playground.sh --headless
```

## 6. 已知注意事项（aarch64 / DGX Spark 专属）

- **conda 冲突**：conda base 激活状态下运行 `isaaclab.sh -p` 会报
  `SRE module mismatch`，先 `conda deactivate`。
- **libgomp 警告**：若出现 `ld.so: ... libgomp ... cannot be preloaded`：
  `export LD_PRELOAD=/lib/aarch64-linux-gnu/libgomp.so.1`
- **驱动版本**：验证过 580.142 正常；避开 595.71.05（Isaac Sim 6.0.1 GPU 崩溃，
  见 NVIDIA 论坛 #376418）。
- **平台限制**：Livestream / Hub 不支持（必须本地显示器）；SkillGen（cuRobo）、
  OpenXR 遥操作不支持。
- **check_environment.sh**：依赖 `docker --version`，本机未装 Docker 会在该处
  报错退出；Isaac 路线不需要 Docker，属检查脚本自身的硬依赖，可忽略或装 Docker。
- **onnxruntime CPU-only**：策略推理在 CPU，物理仿真在 GPU，互不影响。

## 7. 目录速查

```
~/isaacsim-6.0.1                    # Isaac Sim 6.0.1 standalone（26 GB）
~/rlgpu_ws/IsaacLab                 # Isaac Lab v3.0.0-beta2
~/rlgpu_ws/IsaacLab/_isaac_sim      # -> ~/isaacsim-6.0.1（关键软链）
~/microduck-ros2-isaac              # microduck 项目
  ├── reference/                    # fetch_upstream.sh 拉取的上游策略与资产
  └── work/isaac_python_pkgs/       # 项目本地 onnxruntime（不污染环境）
~/Desktop/xj_code/                  # 旧环境（Isaac Sim 5.1 + IsaacLab main），未受影响
```
