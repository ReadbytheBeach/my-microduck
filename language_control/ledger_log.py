#!/usr/bin/env python3
"""Hash-chained event ledger for language control, reusing
scripts/foot_structure_ledger.py.

Event types: intent / proposal / gate / execution / cancel /
capability-gap. Secrets never enter the ledger: environment dicts are
dropped, authorization headers and bearer tokens are redacted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from typing import Any


_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import foot_structure_ledger as _ledger  # noqa: E402

EVENT_TYPES = (
    "intent",
    "proposal",
    "gate",
    "execution",
    "cancel",
    "capability-gap",
    # M1 session service events
    "session",
    "command",
    "ack",
    "sequence",
    "fault",
    # M2 web voice input
    "asr",
)

_SECRET_KEY_RE = re.compile(r"(key|secret|token|password|credential|authorization|env)", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = _sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer <redacted>", value)
    return value


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, event_type: str, **fields) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event_type!r}")
        event = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            **_sanitize(fields),
        }
        return _ledger.append_event(self.path, event)

    def verify(self) -> list[str]:
        return _ledger.verify_chain(self.path)
