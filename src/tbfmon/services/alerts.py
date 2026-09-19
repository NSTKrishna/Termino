"""Alerts: generation from analyses, SQLite persistence, dedupe, cooldown, acknowledgement.

Severities: INFO < WARN < ALARM < CRITICAL.
* dedupe   — while an alert with the same key is open, repeats only bump `occurrences`
             and `last_seen_at`; a higher severity escalates it (and notifies again)
* cooldown — after an acknowledgement, the same key is suppressed for
             `alerts.cooldown_hours` unless the severity rises
* channels — terminal (the caller prints outcomes), JSONL log, email/SMS/webhook stubs
Times are stored as ISO-8601 with the configured timezone offset (IST by default).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .errors import SEVERITY_EXIT, SEVERITY_RANK, TbfmonError
from .notifiers import append_jsonl, notifiers
from .workspace import Workspace

SEVERITIES = ("INFO", "WARN", "ALARM", "CRITICAL")
HEALTH_SEVERITY = {"Critical": "CRITICAL", "Degraded": "WARN"}


@dataclass
class AlertCandidate:
    key: str
    fan_id: str | None
    severity: str
    title: str
    message: str
    source: str
    run_ts: str | None = None


@dataclass
class AlertOutcome:
    alert_id: int | None
    key: str
    fan_id: str | None
    severity: str
    title: str
    message: str
    outcome: str                 # new | escalated | deduplicated | suppressed
    notified: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:g}"


def candidates_from_analysis(an: dict[str, Any], source: str) -> list[AlertCandidate]:
    """Deviation from specified (permissible) values → WARN/ALARM; engine verdict → WARN/CRITICAL;
    suspect readings → INFO (re-measure)."""
    fan = an["fan_id"]
    out: list[AlertCandidate] = []
    for p in an["params"]:
        if p["status"] in ("WARN", "ALARM"):
            dev = "" if p["deviation_pct"] is None else f", {p['deviation_pct']:+.0f}% beyond WARN"
            out.append(AlertCandidate(
                f"{fan}|limit|{p['parameter']}", fan, p["status"], f"{fan} {p['label']} {p['status']}",
                f"{p['label']} measured {_fmt(p['measured'])} {p['unit']} vs limit warn {_fmt(p['warn'])} / "
                f"alarm {_fmt(p['alarm'])}{dev}", source, an["run_ts"]))
        elif p["status"] == "SUSPECT":
            out.append(AlertCandidate(
                f"{fan}|suspect|{p['parameter']}", fan, "INFO", f"{fan} {p['label']} suspect reading",
                f"{p['label']} = {_fmt(p['measured'])} {p['unit']} jumps sharply vs history — re-measure", source,
                an["run_ts"]))
    sev = HEALTH_SEVERITY.get(an["status"])
    named = [m for m in an["fault_modes"] if m["confidence"] >= 0.5]
    if sev is None and an["status"] == "Watch" and named:
        sev = "INFO"
    if sev:
        mode = f"; likely {named[0]['mode']} ({named[0]['confidence'] * 100:.0f}%)" if named else ""
        rec = an["recommendation"]
        out.append(AlertCandidate(
            f"{fan}|health", fan, sev, f"{fan} health {an['status']} ({an['health_index']:.0f}/100)",
            f"Health index {an['health_index']:.0f}{mode}. Action: {rec['action']}"
            + (f" by {rec['by_date']}" if rec["by_date"] else ""), source, an["run_ts"]))
    return out


class AlertStore:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.conn = ws.conn
        self.settings = ws.settings

    # ---- raising ------------------------------------------------------------------------------
    def raise_many(self, cands: Iterable[AlertCandidate], now: dt.datetime | None = None) -> list[AlertOutcome]:
        now = now or self.settings.now()
        outs = [self._raise(c, now) for c in cands]
        self.conn.commit()
        return outs

    def _raise(self, c: AlertCandidate, now: dt.datetime) -> AlertOutcome:
        if c.severity not in SEVERITIES:
            raise ValueError(f"bad severity {c.severity}")
        ts = now.isoformat()
        last = self.conn.execute("SELECT * FROM alert WHERE key = ? ORDER BY id DESC LIMIT 1", (c.key,)).fetchone()
        rank = SEVERITY_RANK[c.severity]
        if last is not None and last["status"] == "open":
            if rank > SEVERITY_RANK[last["severity"]]:
                self.conn.execute("UPDATE alert SET severity=?, title=?, message=?, last_seen_at=?, "
                                  "occurrences=occurrences+1, run_ts=? WHERE id=?",
                                  (c.severity, c.title, c.message, ts, c.run_ts, last["id"]))
                return self._notify(AlertOutcome(last["id"], c.key, c.fan_id, c.severity, c.title, c.message,
                                                 "escalated"), now)
            self.conn.execute("UPDATE alert SET last_seen_at=?, occurrences=occurrences+1 WHERE id=?", (ts, last["id"]))
            return AlertOutcome(last["id"], c.key, c.fan_id, last["severity"], last["title"], last["message"],
                                "deduplicated")
        if last is not None and last["status"] == "acked" and last["acked_at"]:
            since = now - dt.datetime.fromisoformat(last["acked_at"])
            if since < dt.timedelta(hours=self.settings.alerts.cooldown_hours) and rank <= SEVERITY_RANK[last["severity"]]:
                self.conn.execute("UPDATE alert SET last_seen_at=?, occurrences=occurrences+1 WHERE id=?",
                                  (ts, last["id"]))
                return AlertOutcome(last["id"], c.key, c.fan_id, c.severity, c.title, c.message, "suppressed")
        cur = self.conn.execute(
            "INSERT INTO alert(key, fan_id, severity, source, title, message, run_ts, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c.key, c.fan_id, c.severity, c.source, c.title, c.message, c.run_ts, ts, ts))
        return self._notify(AlertOutcome(cur.lastrowid, c.key, c.fan_id, c.severity, c.title, c.message, "new"), now)

    def _notify(self, o: AlertOutcome, now: dt.datetime) -> AlertOutcome:
        a = self.settings.alerts
        record = {"at": now.isoformat(), "event": o.outcome, **o.to_dict()}
        append_jsonl(self.ws.home.root / a.jsonl_log, record)
        o.notified.append("terminal")
        o.notified.append(f"jsonl: {a.jsonl_log}")
        if SEVERITY_RANK[o.severity] >= SEVERITY_RANK.get(a.min_notify_severity, 1):
            subject = f"[tbfmon {o.severity}] {o.title}"
            for n in notifiers(self.settings, self.ws.home.root):
                o.notified.append(n.send(subject, o.message, {"id": o.alert_id, **record}, now.isoformat()))
        return o

    # ---- reading / acknowledging ----------------------------------------------------------------
    def list(self, status: str = "open", fan: str | None = None, min_severity: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        if status not in ("open", "acked", "all"):
            raise TbfmonError(f"unknown status {status!r}", "use open, acked or all")
        q, args = "SELECT * FROM alert WHERE 1=1", []
        if status != "all":
            q += " AND status = ?"
            args.append(status)
        if fan:
            q += " AND fan_id = ?"
            args.append(self.ws.fan_id(fan))
        rows = [dict(r) for r in self.conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]
        if min_severity:
            ms = min_severity.upper()
            if ms not in SEVERITY_RANK:
                raise TbfmonError(f"unknown severity {min_severity!r}", f"use one of {', '.join(SEVERITIES)}")
            rows = [r for r in rows if SEVERITY_RANK[r["severity"]] >= SEVERITY_RANK[ms]]
        rows.sort(key=lambda r: (-SEVERITY_RANK[r["severity"]], -r["id"]))
        return rows

    def ack(self, ids: list[int], user: str | None = None, note: str | None = None, all_open: bool = False,
            fan: str | None = None, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        now = now or self.settings.now()
        user = user or self.settings.who()
        if all_open:
            ids = [r["id"] for r in self.list("open", fan=fan)]
        if not ids:
            raise TbfmonError("nothing to acknowledge", "pass alert IDs (see `tbfmon alerts list`) or --all")
        done = []
        for i in ids:
            row = self.conn.execute("SELECT * FROM alert WHERE id = ?", (i,)).fetchone()
            if row is None:
                raise TbfmonError(f"no alert with id {i}", "see `tbfmon alerts list --status all`")
            if row["status"] == "acked":
                done.append({**dict(row), "result": "already acknowledged"})
                continue
            self.conn.execute("UPDATE alert SET status='acked', acked_by=?, acked_at=?, ack_note=? WHERE id=?",
                              (user, now.isoformat(), note, i))
            append_jsonl(self.ws.home.root / self.settings.alerts.jsonl_log,
                         {"at": now.isoformat(), "event": "acked", "alert_id": i, "by": user, "note": note})
            done.append({**dict(self.conn.execute("SELECT * FROM alert WHERE id = ?", (i,)).fetchone()),
                         "result": "acknowledged"})
        self.conn.commit()
        return done


def alerts_exit(rows: Iterable[dict[str, Any]]) -> int:
    return max((SEVERITY_EXIT[r["severity"]] for r in rows), default=0)


def outcomes_exit(outs: Iterable[AlertOutcome]) -> int:
    return max((SEVERITY_EXIT[o.severity] for o in outs if o.outcome != "suppressed"), default=0)
