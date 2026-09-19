"""Alert channels: JSONL log (always) and email / SMS / webhook stubs.

The stubs never touch the network (DESIGN.md §0: fully local). In dry-run mode
they append the message they *would* send to the outbox JSONL file; with dry-run
off they report "not configured" so wiring a real transport is an explicit,
reviewed change (implement `send` in a subclass).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import NotifierCfg, Settings


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@dataclass
class StubNotifier:
    channel: str
    cfg: NotifierCfg
    outbox: Path

    def send(self, subject: str, body: str, alert: dict[str, Any], now_iso: str) -> str:
        if not self.cfg.enabled:
            return f"{self.channel}: disabled"
        if self.cfg.dry_run:
            append_jsonl(self.outbox, {"at": now_iso, "channel": self.channel, "to": self.cfg.target,
                                       "subject": subject, "body": body, "alert_id": alert.get("id"), "dry_run": True})
            return f"{self.channel}: dry-run → {self.outbox.name}"
        return f"{self.channel}: NOT SENT (stub — no transport configured)"


def notifiers(settings: Settings, home_root: Path) -> list[StubNotifier]:
    outbox = home_root / settings.alerts.outbox
    a = settings.alerts
    return [StubNotifier("email", a.email, outbox), StubNotifier("sms", a.sms, outbox),
            StubNotifier("webhook", a.webhook, outbox)]
