#!/usr/bin/env python3
"""Terminal loop for language control M0.

Execution runs in the executor's monitor thread, so input stays readable
at all times ("停止" is always reachable and handled locally — never via
Kimi). Feedback reports three judgements separately: command constructed /
simulation process completed / motion-target evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time

from .adapters import build_template
from .executor import Executor
from .gateway import Gateway
from .intent import IntentParser, KimiClient, STOP_PHRASES
from .ledger_log import Ledger
from .proposal import ProposalError, new_proposal, Step


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = PROJECT_ROOT / "work/language_control/skill_registry.json"
DEFAULT_LEDGER = PROJECT_ROOT / "work/language_control/events.jsonl"
DEFAULT_WORK_DIR = PROJECT_ROOT / "work/language_control"


def load_registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LanguageControlApp:
    def __init__(
        self,
        registry_path: Path = DEFAULT_REGISTRY,
        ledger_path: Path = DEFAULT_LEDGER,
        work_dir: Path = DEFAULT_WORK_DIR,
        execute: bool = False,
        experimental: bool = False,
        research: bool = False,
        use_llm: bool = False,
    ):
        self.registry = load_registry(registry_path)
        self.ledger = Ledger(ledger_path)
        self.work_dir = work_dir
        self.execute = execute
        self.gateway = Gateway(self.registry, experimental=experimental, research=research)
        self.parser = IntentParser()
        self.llm = KimiClient() if use_llm else None
        self.executor = Executor(
            on_event=self._on_executor_event,
            log_dir=work_dir / "logs",
        )
        self._print_lock = threading.Lock()

    # -- executor events -> ledger --------------------------------------
    def _on_executor_event(self, kind: str, fields: dict) -> None:
        self.ledger.append(kind, **fields)
        if fields.get("stage") in ("done", "failed", "cancelled"):
            with self._print_lock:
                result = self.executor.result or {}
                print("\n[回放结束] 三判断分列:", flush=True)
                print(f"  命令构造正确: {result.get('command_constructed')}", flush=True)
                print(f"  仿真进程正常完成: {result.get('process_completed')}", flush=True)
                print(f"  动作目标证据: {result.get('action_evidence')}", flush=True)
                print(f"  结论: {result.get('overall')}", flush=True)

    # -- one instruction -------------------------------------------------
    def handle_text(self, text: str) -> dict:
        normalized = "".join(text.split())

        # stop is local and immediate — never through Kimi, never queued.
        if normalized in STOP_PHRASES:
            outcome = self.executor.cancel(reason=f"local stop: {text!r}")
            self.ledger.append("intent", raw_text=text, parsed_skill="stop", local=True)
            return {"handled": "stop", **outcome}

        try:
            if self.llm is not None:
                proposal = self.llm.parse(text)
            else:
                proposal = self.parser.parse(text)
        except ProposalError as exc:
            self.ledger.append(
                "intent", raw_text=text, classification=exc.classification, reason=exc.reason
            )
            if exc.classification == "capability_gap":
                self.ledger.append("capability-gap", raw_text=text, reason=exc.reason)
            return {"handled": "rejected", "classification": exc.classification, "reason": exc.reason}

        self.ledger.append(
            "intent",
            raw_text=text,
            parsed_skill=proposal.steps[0].skill,
            source=proposal.source,
        )
        self.ledger.append("proposal", **proposal.to_dict())

        if proposal.steps[0].skill == "stop":
            outcome = self.executor.cancel(reason=f"stop skill: {text!r}")
            return {"handled": "stop", **outcome}

        gate = self.gateway.check(proposal, execute=self.execute)
        self.ledger.append(
            "gate",
            proposal_id=proposal.proposal_id,
            skill=proposal.steps[0].skill,
            execute_requested=self.execute,
            **gate.to_dict(),
        )
        if not gate.allowed:
            return {"handled": "rejected", "classification": gate.classification, "reason": gate.reason}

        skill = proposal.steps[0].skill
        record = dict(self.registry["skills"][skill])
        record["run_id"] = proposal.proposal_id
        try:
            template = build_template(skill, record, proposal.steps[0].params, self.work_dir)
        except ValueError as exc:
            self.ledger.append(
                "capability-gap",
                raw_text=text,
                skill=skill,
                reason=f"local interface gap: {exc}",
            )
            return {"handled": "rejected", "classification": "capability_gap", "reason": str(exc)}

        result = self.executor.submit(proposal.proposal_id, template, execute=self.execute)
        result["labels"] = gate.labels
        result["handled"] = "submitted" if result.get("accepted") else "rejected"
        if gate.dry_run_only and not self.execute:
            result["note"] = "registered skill: dry-run only (use --execute where permitted)"
        if gate.labels.get("research_replay"):
            result["note"] = "research replay: evidence label retained"
        return result

    # -- REPL ------------------------------------------------------------
    def repl(self) -> int:
        mode = "execute" if self.execute else "dry-run"
        print(f"语言控制 M0（模式: {mode}）。输入指令，'停止' 即时取消，'退出' 结束。")
        while True:
            try:
                text = input("microduck> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text in ("退出", "exit", "quit"):
                break
            result = self.handle_text(text)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if self.executor.state in ("starting", "running"):
            print("退出前取消进行中的回放…")
            self.executor.cancel(reason="cli shutdown")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--execute", action="store_true", help="actually start replay processes")
    parser.add_argument("--experimental", action="store_true", help="allow experimental skills")
    parser.add_argument("--research", action="store_true",
                        help="research replay of pending/failed skills (label retained)")
    parser.add_argument("--llm", action="store_true", help="parse via Kimi instead of the rule grammar")
    parser.add_argument("--once", type=str, default=None, help="handle one instruction and exit")
    parser.add_argument("--wait", action="store_true", help="with --once --execute, wait for completion")
    args = parser.parse_args(argv)

    app = LanguageControlApp(
        registry_path=args.registry,
        ledger_path=args.ledger,
        work_dir=args.work_dir,
        execute=args.execute,
        experimental=args.experimental,
        research=args.research,
        use_llm=args.llm,
    )
    if args.once is not None:
        result = app.handle_text(args.once)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.wait:
            while app.executor.state in ("starting", "running"):
                time.sleep(0.2)
            final = app.executor.result
            if final is not None:
                print(json.dumps(final, ensure_ascii=False, indent=2))
                return 0 if final.get("state") == "done" else 1
        return 0 if result.get("handled") != "rejected" else 1
    return app.repl()


if __name__ == "__main__":
    sys.exit(main())
