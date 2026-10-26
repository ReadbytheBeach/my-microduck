#!/usr/bin/env python3
"""Hash-chained event ledger for the V5 foot-structure experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator


def canonical_bytes(event: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(event)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            text = line.strip()
            if text:
                yield json.loads(text)


def append_event(path: Path, event: dict[str, Any]) -> dict[str, Any]:
    seq = 1
    prev_hash = None
    if path.is_file():
        last: dict[str, Any] | None = None
        for last in iter_events(path):
            pass
        if last is not None:
            seq = last["seq"] + 1
            prev_hash = last["event_hash"]
    chained = {**event, "seq": seq, "prev_hash": prev_hash}
    chained["event_hash"] = event_digest(chained)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(chained, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return chained


def verify_chain(path: Path) -> list[str]:
    if not path.is_file():
        return [f"ledger not found: {path}"]
    errors: list[str] = []
    expected_seq = 1
    prev_hash: str | None = None
    with path.open("r", encoding="utf-8") as stream:
        for lineno, line in enumerate(stream, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: invalid JSON: {exc}")
                continue
            if not {"seq", "prev_hash", "event_hash"} <= event.keys():
                errors.append(
                    f"line {lineno}: missing chain fields (legacy ledger; run migrate first)"
                )
                prev_hash = None
                continue
            if event["seq"] != expected_seq:
                errors.append(f"line {lineno}: seq {event['seq']} but expected {expected_seq}")
            if event["prev_hash"] != prev_hash:
                errors.append(f"line {lineno}: prev_hash does not match previous event_hash")
            if event["event_hash"] != event_digest(event):
                errors.append(f"line {lineno}: event_hash does not match recomputed digest")
            prev_hash = event["event_hash"]
            expected_seq = event["seq"] + 1 if isinstance(event["seq"], int) else expected_seq + 1
    return errors


def migrate(src: Path, dst: Path) -> int:
    count = 0
    prev_hash: str | None = None
    with src.open("r", encoding="utf-8") as source, dst.open("w", encoding="utf-8") as out:
        for line in source:
            text = line.strip()
            if not text:
                continue
            event = json.loads(text)
            count += 1
            chained = dict(event)
            if count == 1:
                chained["migrated_from_legacy"] = True
            chained["seq"] = count
            chained["prev_hash"] = prev_hash
            chained["event_hash"] = event_digest(chained)
            prev_hash = chained["event_hash"]
            out.write(json.dumps(chained, allow_nan=False) + "\n")
        out.flush()
        os.fsync(out.fileno())
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("ledger", type=Path)
    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("src", type=Path)
    migrate_parser.add_argument("dst", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        errors = verify_chain(args.ledger)
        if errors:
            for error in errors:
                print(f"FAIL {error}")
            print(f"verify failed: {len(errors)} error(s) in {args.ledger}")
            return 1
        count = sum(1 for _ in iter_events(args.ledger))
        print(f"verify ok: {count} event(s) in {args.ledger}")
        return 0
    count = migrate(args.src, args.dst)
    errors = verify_chain(args.dst)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print(f"migrated {count} event(s): {args.src} -> {args.dst} (chain verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
