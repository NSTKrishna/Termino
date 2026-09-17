"""MQTT source — documented stub for future live sensors. Not implemented.

Intended deployment: an on-site gateway (vibration/temperature transmitters,
MCC power meter, SCADA start/stop events) publishes to a local broker in the
station equipment room. tbfmon stays terminal-only: this source would run from
`tbfmon ingest --source mqtt://...` or a cron job, never as a web service.

Topic contract (proposed)
-------------------------
tbf/<station>/<fan_id>/run/start        {"ts": ISO8601, "run_type": "..."}
tbf/<station>/<fan_id>/sample           {"ts": ISO8601, "<measurement>": value, ...}
tbf/<station>/<fan_id>/run/stop         {"ts": ISO8601, "coastdown_time_s": value}
tbf/<station>/<fan_id>/run/summary      one TestRun dict (preferred if the gateway aggregates)

Measurement names and units must match `tbfmon.models.MEASUREMENTS`.

Run aggregation contract (if only raw samples are published)
------------------------------------------------------------
Collect samples between run/start and run/stop, then emit ONE `test_run`
RawRecord with:
  * timestamp      = start ts;  duration_min = (stop - start)
  * vib_* RMS/1x/2x = value over the last 60 s of the run (steady state)
  * temperatures   = last sample before stop (end-of-run)
  * currents/volts = median over the last 60 s
  * inrush_peak_a  = max current in the first 2 s;  runup_time_s from start to 98 % rpm
  * coastdown_time_s from the stop message
The same natural key (fan_id, timestamp, run_type) makes redelivery idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .base import RawRecord


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    topic_root: str = "tbf/#"
    client_id: str = "tbfmon"


class MqttSource:
    def __init__(self, config: MqttConfig | None = None):
        self.config = config or MqttConfig()
        self.name = f"mqtt:{self.config.host}:{self.config.port}/{self.config.topic_root}"

    def records(self) -> Iterator[RawRecord]:
        raise NotImplementedError("MqttSource is a stub; see module docstring for the contract")
