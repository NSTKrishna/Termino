"""Modbus/TCP source — documented stub for future PLC/DDC polling. Not implemented.

Intended use: poll the station DDC/PLC (or a condition-monitoring transmitter)
during a test run and hand the aggregated run to ingestion as one `test_run`
record, following the aggregation contract in `mqtt_stub`.

Register map (proposed, per fan; holding registers, big-endian, 16-bit)
-----------------------------------------------------------------------
offset  measurement                 scale  unit
0       vib_de_rms_mms              0.01   mm/s
1       vib_nde_rms_mms             0.01   mm/s
2       temp_bearing_de_c           0.1    °C
3       temp_bearing_nde_c          0.1    °C
4       temp_winding_c              0.1    °C
5..7    current_l1_a..current_l3_a  0.1    A
8       voltage_v                   1      V
9       rpm                         1      rpm
10      ambient_temp_c              0.1    °C
20      run status bitfield (bit0 running, bit1 tripped, bit2 local/remote)

Each fan's block starts at `base_register + fan_slot * block_size`.
1x/2x amplitudes, airflow, IR and noise are not usually on Modbus and remain
manual (CSV) entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from .base import RawRecord


@dataclass
class ModbusConfig:
    host: str = "192.168.0.10"
    port: int = 502
    unit_id: int = 1
    base_register: int = 0
    block_size: int = 32
    fan_slots: dict[str, int] = field(default_factory=dict)  # fan_id -> slot
    poll_interval_s: float = 1.0


class ModbusSource:
    def __init__(self, config: ModbusConfig | None = None):
        self.config = config or ModbusConfig()
        self.name = f"modbus:{self.config.host}:{self.config.port}/unit{self.config.unit_id}"

    def records(self) -> Iterator[RawRecord]:
        raise NotImplementedError("ModbusSource is a stub; see module docstring for the register map")
