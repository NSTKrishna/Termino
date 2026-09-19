"""Workspace settings: config.yaml in the home directory, overridable by environment.

Environment overrides: TBFMON_TZ, TBFMON_USER, TBFMON_ALERT_COOLDOWN_HOURS,
TBFMON_NOTIFY_DRY_RUN (applies to every notifier), TBFMON_WATCH_INTERVAL.
Stored timestamps of readings are naive local time; the configured timezone
(default IST, Asia/Kolkata) is used for display and for alert/ack times.
"""

from __future__ import annotations

import datetime as dt
import getpass
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import TbfmonError

DEFAULT_CONFIG = """\
# tbfmon workspace settings (environment variables override these; see README)
timezone: Asia/Kolkata          # IST; TBFMON_TZ
user: null                      # who acknowledges alerts; TBFMON_USER, else the OS user
watch_interval_s: 2.0           # polling interval for `tbfmon watch`; TBFMON_WATCH_INTERVAL
alerts:
  cooldown_hours: 12            # an acknowledged alert is not re-raised within this window
  jsonl_log: alerts.jsonl       # every raised/escalated alert is appended here (relative to home)
  outbox: outbox.jsonl          # dry-run notifier messages are written here
  email:   {enabled: false, dry_run: true, target: "maintenance@example.invalid"}
  sms:     {enabled: false, dry_run: true, target: "+91-00000-00000"}
  webhook: {enabled: false, dry_run: true, target: "http://localhost/placeholder"}
  min_notify_severity: WARN     # INFO alerts are stored but not pushed to notifiers
"""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotifierCfg(_M):
    enabled: bool = False
    dry_run: bool = True
    target: str = ""


class AlertsCfg(_M):
    cooldown_hours: float = Field(12.0, ge=0)
    jsonl_log: str = "alerts.jsonl"
    outbox: str = "outbox.jsonl"
    email: NotifierCfg = NotifierCfg()
    sms: NotifierCfg = NotifierCfg()
    webhook: NotifierCfg = NotifierCfg()
    min_notify_severity: str = "WARN"


class Settings(_M):
    timezone: str = "Asia/Kolkata"
    user: str | None = None
    watch_interval_s: float = Field(2.0, gt=0)
    alerts: AlertsCfg = AlertsCfg()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz).replace(microsecond=0)

    def who(self) -> str:
        if self.user:
            return self.user
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001 - no login name in some containers
            return "unknown"

    def fmt(self, ts: dt.datetime | str | None) -> str:
        """Human timestamp in the configured zone, e.g. '2025-12-31 02:14 IST'."""
        if ts is None:
            return "—"
        t = dt.datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        t = t.replace(tzinfo=self.tz) if t.tzinfo is None else t.astimezone(self.tz)
        return f"{t:%Y-%m-%d %H:%M} {t.tzname()}"


def _env_bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_settings(home_root: Path) -> Settings:
    path = home_root / "config.yaml"
    data = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise TbfmonError(f"config.yaml is not valid YAML: {e}", f"fix or delete {path}") from e
    env = os.environ
    if env.get("TBFMON_TZ"):
        data["timezone"] = env["TBFMON_TZ"]
    if env.get("TBFMON_USER"):
        data["user"] = env["TBFMON_USER"]
    if env.get("TBFMON_WATCH_INTERVAL"):
        data["watch_interval_s"] = float(env["TBFMON_WATCH_INTERVAL"])
    alerts = data.setdefault("alerts", {}) or {}
    data["alerts"] = alerts
    if env.get("TBFMON_ALERT_COOLDOWN_HOURS"):
        alerts["cooldown_hours"] = float(env["TBFMON_ALERT_COOLDOWN_HOURS"])
    if env.get("TBFMON_NOTIFY_DRY_RUN"):
        for ch in ("email", "sms", "webhook"):
            alerts.setdefault(ch, {})["dry_run"] = _env_bool(env["TBFMON_NOTIFY_DRY_RUN"])
    try:
        s = Settings.model_validate(data)
        s.tz  # noqa: B018 - validate the zone name early
    except ValidationError as e:
        raise TbfmonError(f"invalid settings in {path}: {e.errors()[0]['msg']} at "
                          f"{'.'.join(str(x) for x in e.errors()[0]['loc'])}") from e
    except ZoneInfoNotFoundError as e:
        raise TbfmonError(f"unknown timezone {data.get('timezone')!r}", "use an IANA name such as Asia/Kolkata") from e
    return s
