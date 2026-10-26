#!/usr/bin/env python3
"""Build the retained Chinese V5 foot-structure experiment report and comparisons."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import foot_structure_ledger


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work/video_dance/v5_foot_structure"
ART = ROOT / "artifacts/isaac/video-dance-foot-v5"
REPORT = ROOT / "work/video_dance/docs/experiments/V5阶段1_足部结构/随地形和摩擦力的结构设计自动化优化.md"
VARIANTS = ("original", "W090", "W115", "W130", "L110", "L110W115", "L110W130")
CASES = (("normal", 0), ("normal", 4), ("marble", 0), ("marble", 4))
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def link(path: Path | str, report: Path) -> str:
    value = Path(path).resolve()
    if not value.exists():
        raise FileNotFoundError(value)
    return Path(os.path.relpath(value, report.parent.resolve())).as_posix()


def md_link(label: str, path: Path | str, report: Path) -> str:
    return f"[{label}]({link(path, report)})"


def make_comparison(seed: int, winner: str, surface: str, slope: int, report: Path) -> Path:
    case = f"{surface}-slope-{slope}"
    base = ART / "acceptance" / f"seed{seed}" / "original" / f"acceptance-{case}.mp4"
    candidate = ART / "acceptance" / f"seed{seed}" / winner / f"acceptance-{case}.mp4"
    output = ART / "review" / f"compare-seed{seed}-{case}.mp4"
    font = f":fontfile={FONT}" if FONT.is_file() else ""
    filter_graph = (
        f"[0:v]scale=960:540,drawtext{font}:text='original':x=24:y=24:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55[left];"
        f"[1:v]scale=960:540,drawtext{font}:text='{winner}':x=24:y=24:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55[right];"
        "[left][right]hstack=inputs=2[v]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(base),
            "-i",
            str(candidate),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-r",
            "25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
        check=True,
        capture_output=True,
    )
    return output


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def status_for_manifest(manifest: dict[str, Any], variant: str, stage: str) -> str:
    return manifest["variants"].get(variant, {}).get("stages", {}).get(stage, {}).get("status", "missing")


def replay_ledger(events: list[dict[str, Any]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {"variants": {}}
    for event in events:
        if event.get("status") == "running":
            continue
        stages = manifest["variants"].setdefault(event["variant"], {}).setdefault("stages", {})
        stages[event["stage"]] = event
    return manifest


def analysis_file(event: dict[str, Any], key: str) -> Path:
    path = Path(event[key])
    recorded = event.get(f"{key}_sha256")
    if recorded is not None and foot_structure_ledger.file_sha256(path) != recorded:
        raise SystemExit(f"ledger {key}_sha256 mismatch: {path}")
    return path


def build_report(report_path: Path) -> None:
    events_path = WORK / "events.jsonl"
    errors = foot_structure_ledger.verify_chain(events_path)
    if errors:
        raise SystemExit(
            f"ledger verification failed for {events_path}; refusing to build report:\n"
            + "\n".join(errors)
        )
    events = list(foot_structure_ledger.iter_events(events_path))
    provenance = read_json(WORK / "manifest.json")
    manifest = {
        "updated_utc": provenance["updated_utc"],
        "parent": provenance["parent"],
        "clip": provenance["clip"],
        "git": provenance["git"],
        "variants": replay_ledger(events)["variants"],
    }
    matrix = read_json(WORK / "variant_matrix.json")
    analyze42 = manifest["variants"].get("original", {}).get("stages", {}).get("analyze-seed42", {})
    if analyze42.get("status") != "success":
        raise SystemExit("ledger has no successful analyze-seed42 event; refusing to build report")
    selection = read_json(analysis_file(analyze42, "selection"))
    summary42 = read_json(analysis_file(analyze42, "summary"))
    winner = selection.get("winner")
    analyze43 = manifest["variants"].get("original", {}).get("stages", {}).get("analyze-seed43", {})
    summary43_path = analysis_file(analyze43, "summary") if analyze43.get("status") == "success" else None
    summary43 = read_json(summary43_path) if summary43_path is not None else None
    confirmation_path = (
        summary43_path.parent / "confirmation_verdict.json"
        if summary43_path is not None
        else ART / "analysis/seed43/confirmation_verdict.json"
    )
    confirmation_event = (
        manifest["variants"].get(winner, {}).get("stages", {}).get("confirmation-seed43", {})
        if winner is not None
        else {}
    )
    confirmation = confirmation_event.get("verdict") if confirmation_event else None
    optimization = manifest["variants"].get("original", {}).get("stages", {}).get("optimization", {})
    no_candidate = optimization.get("verdict", {}).get("status") == "no_eligible_variant"
    if confirmation is not None:
        final_status = "改善通过" if confirmation["passed"] else "筛选候选未获独立种子证实"
    elif winner is None and no_candidate:
        final_status = "未找到合格结构"
    elif winner is not None:
        final_status = "已选出候选，独立复核未完成"
    else:
        final_status = "试验未完成，暂无结论"
    final_seed = 43 if summary43 is not None else 42

    comparisons: dict[str, Path] = {}
    if winner is not None and summary43 is not None:
        for surface in ("normal", "marble"):
            comparisons[surface] = make_comparison(final_seed, winner, surface, 4, report_path)

    lines = [
        "# 随地形和摩擦力的结构设计自动化优化",
        "",
        f"生成时间：{manifest['updated_utc']}  ",
        f"总状态：**{final_status}**  ",
        f"最优候选：`{winner or '无'}`；筛选种子：42；复核种子：43。",
        "",
        "## 1. 目标与结论边界",
        "",
        "本轮在保持 14 个主动关节、61D actor 观测和 14D 动作接口不变的条件下，自动筛选七种刚性鞋底几何。训练与 Isaac 验收只使用普通地面和大理石；坡度验收只取 0°/4°，不测试木地板、柔性鞋底或真机。",
        "",
        "方法参考 MIT [Draft: A Parametric Tool for Robot Design Exploration](https://arxiv.org/abs/2609.38405)：几何变化同步进入碰撞、质量和惯量，所有结构使用相同父策略、训练预算和地形课程，并以独立种子复核。Draft 提供的是参数化结构—控制评估方法，不是本报告某一鞋底形状的直接证据。",
        "",
        "原脚对照也执行相同的 500 轮微调，因此候选与原脚的差异不把额外训练预算混入结构收益。所有失败、缺失数据和未通过门槛均保留，不以 0 分代替。",
        "",
        "## 2. 输入、结构变体与物理假设",
        "",
        f"- 父策略：`{manifest['parent']['run_dir']}/{manifest['parent']['checkpoint']}`，SHA-256 `{manifest['parent']['sha256']}`。",
        f"- V2 参考轨迹：`{manifest['clip']['path']}`，SHA-256 `{manifest['clip']['sha256']}`。",
        f"- 项目版本：主仓库 `{manifest['git']['project']}`，microduck_rl `{manifest['git']['microduck_rl']}`；实验前工作区已有 dirty/untracked 文件，本实验不创建 commit。",
        "- 训练地形：normal/marble 各 50%，0–4° 随机坡向；Isaac 使用 normal 摩擦 1.0、marble 摩擦 0.55，坡向 0°、roughness 0。",
        "- 鞋底等效密度假设为 1200 kg/m³；按网格质量特性计算相对原鞋底的质量、质心和惯量差量。该密度不是实测材料参数，结果不能直接视为实物制造验证。",
        "- Z、踝高、foot site、HOME、关节顺序和参考轨迹保持不变；单脚净增质量限制为 5 g。",
        "",
        md_link("七种鞋底俯视几何图", ART / "review/sole_variants.png", report_path),
        "",
        "| 变体 | L | W | 单脚质量变化(g) | 用途 |",
        "|---|---:|---:|---:|---|",
    ]
    purposes = {
        "original": "同预算原脚对照",
        "W090": "缩窄负对照",
        "W115": "宽度候选",
        "W130": "宽度候选",
        "L110": "长度候选",
        "L110W115": "长宽组合",
        "L110W130": "长宽组合",
    }
    for name in VARIANTS:
        record = matrix["variants"][name]
        scale = record["scale"]
        delta = record["feet"]["left"]["delta_sole"]["mass_kg"] * 1000.0
        lines.append(f"| `{name}` | {scale['L']:.2f} | {scale['W']:.2f} | {delta:+.4f} | {purposes[name]} |")

    lines.extend([
        "",
        "完整网格、惯量和输入/输出哈希见 " + md_link("variant_matrix.json", WORK / "variant_matrix.json", report_path) + "；全部命令和阶段事件见 " + md_link("events.jsonl", WORK / "events.jsonl", report_path) + "。",
        "",
        "## 3. 过程记录：成功与失败",
        "",
        "| 变体 | USD | 64×5 冒烟 | 500 轮训练 | 四场景验收 | 训练耗时(min) | 验收耗时(min) | 最终 Mean reward | fell_over | NaN | 证据 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for name in VARIANTS:
        stages = manifest["variants"].get(name, {}).get("stages", {})
        train_event = stages.get("train-seed42", {})
        metrics = train_event.get("metrics", {})
        acceptance_elapsed = sum(
            event.get("elapsed_s", 0.0)
            for stage, event in stages.items()
            if stage.startswith("accept-seed42-")
        )
        media_event = stages.get("media-train-seed42", {})
        if media_event.get("media"):
            evidence = md_link("训练抽查图", media_event["media"][-1]["review_image"], report_path)
        else:
            evidence = md_link("阶段日志", train_event.get("log", WORK / "events.jsonl"), report_path)
        lines.append(
            f"| `{name}` | {status_for_manifest(manifest, name, 'convert')} | "
            f"{status_for_manifest(manifest, name, 'smoke-seed42')} | "
            f"{status_for_manifest(manifest, name, 'train-seed42')} | "
            f"{status_for_manifest(manifest, name, 'accept-seed42')} | "
            f"{train_event.get('elapsed_s', 0.0) / 60.0:.2f} | {acceptance_elapsed / 60.0:.2f} | "
            f"{fmt(metrics.get('mean_reward'))} | {fmt(metrics.get('fell_over'))} | "
            f"{fmt(metrics.get('nan_state'), 1)} | {evidence} |"
        )
    lines.extend([
        "",
        "进入 Isaac 前的训练门槛为 `nan_state=0`、`fell_over≤0.375`，且候选 `fell_over` 不得比同种子原脚高 0.05 以上。64×5 只用于技术冒烟，其短跑倾倒率不代表长训练最终稳定性。",
        "",
        "## 4. 种子 42 自动选型",
        "",
        "评分公式：`J = 0.75 × mean(Dv/Do) + 0.25 × mean((Sv+0.001)/(So+0.001))`，其中 D 为根部平面位移，S 为双脚 loaded 状态下的脚底 site 累计滑动。候选还须满足 J<1、两个 4° 位移都优于原脚、两个 0° 退化不超过 10%，以及中位幅度、右膝和右踝幅度均不低于原脚的 95%。",
        "",
        "| 变体 | 技术门槛 | J | eligible | 主要原因 |",
        "|---|---|---:|---|---|",
    ])
    for name in VARIANTS:
        record = selection["variants"][name]
        reasons = record["eligibility_reasons"]
        reason = "<br>".join(reasons) if reasons else "通过全部条件"
        lines.append(
            f"| `{name}` | {record['technical_gate_passed']} | {fmt(record['score_j'], 5)} | "
            f"{record['eligible']} | {reason} |"
        )
    lines.extend([
        "",
        f"自动选型结果：`{winner or '无合格候选'}`。机器可读明细见 " + md_link("selection.json", ART / "analysis/seed42/selection.json", report_path) + " 和 " + md_link("summary.json", ART / "analysis/seed42/summary.json", report_path) + "。",
        "",
        md_link("位移对比图", ART / "analysis/seed42/displacement.png", report_path) + "；" +
        md_link("滑动对比图", ART / "analysis/seed42/sliding.png", report_path) + "；" +
        md_link("幅度对比图", ART / "analysis/seed42/amplitude.png", report_path) + "。",
        "",
        "### 原始位移与脚底滑动汇总（种子 42）",
        "",
        "| 变体 | D normal 0° | D normal 4° | D marble 0° | D marble 4° | S normal 4° | S marble 4° |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for name in VARIANTS:
        values = []
        for surface, slope in CASES:
            case = f"{surface}-slope-{slope}"
            entry = summary42["variants"][name]["cases"][case]
            if entry.get("status") == "ok":
                values.append(entry["analysis"]["root_planar_displacement_m"])
            else:
                values.append(None)
        slides = []
        for surface in ("normal", "marble"):
            case = f"{surface}-slope-4"
            entry = summary42["variants"][name]["cases"][case]
            slides.append(
                entry["analysis"]["total_slide_distance_m"] if entry.get("status") == "ok" else None
            )
        lines.append(
            f"| `{name}` | {fmt(values[0], 5)} | {fmt(values[1], 5)} | "
            f"{fmt(values[2], 5)} | {fmt(values[3], 5)} | {fmt(slides[0], 5)} | {fmt(slides[1], 5)} |"
        )
    lines.append("")
    for name in VARIANTS:
        accept_event = manifest["variants"].get(name, {}).get("stages", {}).get("accept-seed42", {})
        if accept_event.get("status") != "failed":
            continue
        reasons = "、".join(accept_event.get("gate_reasons", ["unspecified"]))
        metrics = accept_event.get("training_metrics", {})
        baseline = accept_event.get("baseline_training_metrics", {})
        lines.append(
            f"- `{name}` 未进入 Isaac 回放：{reasons}；fell_over={metrics.get('fell_over')}，"
            f"同种子原脚={baseline.get('fell_over')}。缺失验收文件是主动拦截，不是数据丢失。"
        )
    lines.extend([
        "",
        f"## 5. 原脚与候选的四场景结果（种子 {final_seed}）",
        "",
    ])
    final_summary = summary43 if summary43 is not None else summary42
    if winner is not None:
        lines.extend([
            "| 场景 | 原脚 D(m) | 候选 D(m) | D 比值 | 原脚 S(m) | 候选 S(m) | 候选中位幅度 | 非预期 reset | 视频 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ])
        for surface, slope in CASES:
            case = f"{surface}-slope-{slope}"
            original = final_summary["variants"]["original"]["cases"][case]["analysis"]
            candidate = final_summary["variants"][winner]["cases"][case]["analysis"]
            d0 = original["root_planar_displacement_m"]
            d1 = candidate["root_planar_displacement_m"]
            s0 = original["total_slide_distance_m"]
            s1 = candidate["total_slide_distance_m"]
            amplitude = candidate["amplitude"]["median_amplitude_ratio"]
            resets = original["unexpected_reset_count"] + candidate["unexpected_reset_count"]
            base_video = ART / "acceptance" / f"seed{final_seed}/original/acceptance-{case}.mp4"
            candidate_video = ART / "acceptance" / f"seed{final_seed}/{winner}/acceptance-{case}.mp4"
            videos = md_link("原脚", base_video, report_path) + " / " + md_link("候选", candidate_video, report_path)
            lines.append(
                f"| {case} | {d0:.5f} | {d1:.5f} | {d1/d0:.4f} | {s0:.5f} | {s1:.5f} | "
                f"{amplitude:.4f} | {resets} | {videos} |"
            )
        if comparisons:
            lines.extend([
                "",
                "并排对照视频：" + "；".join(
                    md_link(f"{surface} 4°", path, report_path) for surface, path in comparisons.items()
                ) + "。",
            ])
    else:
        lines.append("没有候选满足种子 42 的全部选型条件，因此不生成原脚与候选的对比表，也不把某个最低分变体包装成成功结构。")
    lines.extend([
        "",
        "## 6. 独立种子复核与最终判定",
        "",
    ])
    if confirmation is not None:
        lines.append(f"复核状态：**{confirmation['status']}**；winner=`{winner}`。")
        lines.append("")
        lines.append("| 场景 | D 比值 | 原脚 D(m) | 候选 D(m) | 通过 | 原因 |")
        lines.append("|---|---:|---:|---:|---|---|")
        for case, check in confirmation["cases"].items():
            lines.append(
                f"| {case} | {fmt(check.get('displacement_ratio'))} | "
                f"{fmt(check.get('original_displacement_m'), 5)} | {fmt(check.get('candidate_displacement_m'), 5)} | "
                f"{check['passed']} | {'<br>'.join(check['reasons']) or '通过'} |"
            )
        lines.extend([
            "",
            "最终通过要求：normal/marble 4° 位移均至少降低 10% 且低于 0.25 m，0° 退化不超过 10%，无非预期 reset，幅度门槛继续通过。",
            "",
            "复核明细：" + md_link("confirmation_verdict.json", confirmation_path, report_path) + "；" + md_link("种子 43 summary", summary43_path, report_path) + "。",
        ])
    elif winner is None:
        lines.append("种子 42 没有 eligible 候选，未进入种子 43。最终判定为“未找到合格结构”，不建议据此制造任一候选。")
    else:
        lines.append("种子 43 尚未形成完整结果，本报告只能标记为复核未完成，不能宣称结构改善已证实。")
    lines.extend([
        "",
        "若 D 下降而 S 未下降，只能说明支撑几何可能改善了运动结果，不能宣称防滑机制已被证明。脚底 S 是 site 位移代理，不是完整接触斑或 COP 测量。",
        "",
        "## 7. 限制与后续",
        "",
        "- 本轮只改变刚性鞋底平面几何；不包含被动踝、TPU 顺应、头部减重或腿部改造。",
        "- 鞋底密度为显式近似，实物前必须确认材料、称重并重新计算惯量。",
        "- 种子 42 用于筛选、种子 43 仅用于原脚与最优候选复核，仍不是论文式十种子的统计结论。",
        "- mjlab 使用 BAM 电压执行器，Isaac 使用隐式 PD；结论要求跨仿真趋势一致，但两套物理并不相同。",
        "- 验收为固定坡向、单环境、32 秒回放；尚未覆盖多种子 Isaac 统计、随机坡向验收或真机。",
        "- 若全部候选失败，下一步应先依据脚底轨迹区分主动跨步、支撑滑动和边缘接触，再决定是否转向头颈质心或控制优化，而不是继续扩大鞋底。",
        "",
        "## 8. 可复现入口",
        "",
        "```bash",
        "cd /home/xj/microduck-ros2-isaac/work/dance/microduck_rl",
        "./.venv/bin/python scripts/generate_foot_variants.py",
        "cd /home/xj/microduck-ros2-isaac",
        "work/dance/microduck_rl/.venv/bin/python scripts/run_foot_structure_optimization.py pipeline",
        "work/dance/microduck_rl/.venv/bin/python scripts/foot_structure_ledger.py verify work/video_dance/v5_foot_structure/events.jsonl",
        "work/dance/microduck_rl/.venv/bin/python scripts/build_foot_structure_report.py",
        "```",
        "",
        "注：生成器的实际工作目录为 `work/dance/microduck_rl`；每个变体的完整 argv、环境覆盖、日志、耗时和产物哈希均以 `manifest.json` 与 `events.jsonl` 为准。",
    ])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPORT)
    args = parser.parse_args()
    build_report(args.output.resolve())
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
