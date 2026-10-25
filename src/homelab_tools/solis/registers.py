"""Decode Solis hybrid-inverter input registers into a :class:`Reading`.

Register map: Solis single-phase hybrid (RHI / S5 / S6 hybrid) "Modbus communication protocol",
input registers read with function code 4. Values verified against a live inverter; in particular
the meter power sign (positive = exporting) was checked by comparing inverter AC output minus house
load with the meter reading.

=========  ======================================================  ==========
Register   Meaning                                                 Scale
=========  ======================================================  ==========
33029-30   lifetime PV generation (u32)                            1 kWh
33031-32   PV generation this month (u32)                          1 kWh
33035      PV generation today                                     0.1 kWh
33049-52   DC string 1 voltage, current, string 2 voltage, current 0.1 V / A
33057-58   total DC (PV) power (u32)                               1 W
33093      inverter temperature                                    0.1 °C
33116-20   fault codes 1-5 (0 = none)
33128      grid voltage                                            0.1 V
33130-31   meter active power (s32), + = export, - = import        1 W
33135      battery current direction: 0 charging, 1 discharging
33139      battery state of charge                                 %
33140      battery state of health                                 %
33147      house load power                                        1 W
33149-50   battery power (magnitude; sign from 33135)              1 W
33163/67   battery charged / discharged today                      0.1 kWh
33171/75   grid import / export today                              0.1 kWh
33179      house consumption today                                 0.1 kWh
=========  ======================================================  ==========
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: (first register, count) blocks that together cover every register decoded below. Two reads keep
#: each request within Modbus' 125-register limit and are gentle on Wi-Fi data loggers.
REGISTER_BLOCKS: tuple[tuple[int, int], ...] = ((33022, 74), (33116, 64))


def u32(regs: Mapping[int, int], address: int) -> int:
    """Combine two consecutive 16-bit registers (high word first) into an unsigned 32-bit value."""
    return (regs[address] << 16) | regs[address + 1]


def s32(regs: Mapping[int, int], address: int) -> int:
    """Like :func:`u32` but interpreted as a two's-complement signed value."""
    value = u32(regs, address)
    return value - (1 << 32) if value & 0x80000000 else value


@dataclass(frozen=True)
class Reading:
    """One snapshot of the inverter, in plain units (W, kWh, °C, %).

    Sign conventions: ``battery_w`` is positive while charging and negative while discharging;
    ``grid_w`` is positive while exporting and negative while importing.
    """

    pv_w: int
    pv_today_kwh: float
    pv_month_kwh: int
    pv_total_kwh: int
    load_w: int
    load_today_kwh: float
    soc_pct: int
    soh_pct: int
    battery_w: int
    battery_in_today_kwh: float
    battery_out_today_kwh: float
    grid_w: int
    grid_in_today_kwh: float
    grid_out_today_kwh: float
    grid_v: float
    inverter_temp_c: float
    dc1_v: float
    dc1_a: float
    dc2_v: float
    dc2_a: float
    fault_codes: tuple[int, ...]

    @property
    def has_fault(self) -> bool:
        """Whether the inverter currently reports any fault code."""
        return bool(self.fault_codes)


def decode(regs: Mapping[int, int]) -> Reading:
    """Turn a register map (as returned by Modbus reads of :data:`REGISTER_BLOCKS`) into a Reading.

    Raises:
        KeyError: If a required register is missing from ``regs``.
    """
    battery_magnitude = abs(s32(regs, 33149))
    discharging = regs[33135] == 1
    return Reading(
        pv_w=u32(regs, 33057),
        pv_today_kwh=regs[33035] / 10,
        pv_month_kwh=u32(regs, 33031),
        pv_total_kwh=u32(regs, 33029),
        load_w=regs[33147],
        load_today_kwh=regs[33179] / 10,
        soc_pct=regs[33139],
        soh_pct=regs[33140],
        battery_w=-battery_magnitude if discharging else battery_magnitude,
        battery_in_today_kwh=regs[33163] / 10,
        battery_out_today_kwh=regs[33167] / 10,
        grid_w=s32(regs, 33130),
        grid_in_today_kwh=regs[33171] / 10,
        grid_out_today_kwh=regs[33175] / 10,
        grid_v=regs[33128] / 10,
        inverter_temp_c=regs[33093] / 10,
        dc1_v=regs[33049] / 10,
        dc1_a=regs[33050] / 10,
        dc2_v=regs[33051] / 10,
        dc2_a=regs[33052] / 10,
        fault_codes=tuple(regs[a] for a in range(33116, 33121) if regs[a]),
    )


def metric_fields(r: Reading) -> dict[str, float | int]:
    """Numbers for the time-series database (metric names become ``solis_<key>``)."""
    return {
        "pv_w": r.pv_w,
        "load_w": r.load_w,
        "soc": r.soc_pct,
        "soh": r.soh_pct,
        "batt_w": r.battery_w,
        "grid_w": r.grid_w,
        "pv_today_kwh": r.pv_today_kwh,
        "pv_total_kwh": r.pv_total_kwh,
        "load_today_kwh": r.load_today_kwh,
        "batt_in_today_kwh": r.battery_in_today_kwh,
        "batt_out_today_kwh": r.battery_out_today_kwh,
        "grid_in_today_kwh": r.grid_in_today_kwh,
        "grid_out_today_kwh": r.grid_out_today_kwh,
        "inv_temp_c": r.inverter_temp_c,
        "grid_v": r.grid_v,
        "dc1_v": r.dc1_v,
        "dc1_a": r.dc1_a,
        "dc2_v": r.dc2_v,
        "dc2_a": r.dc2_a,
    }


def watts(value: int) -> str:
    """Format a power value: ``"640 W"`` below 1 kW, ``"1.25 kW"`` above."""
    return f"{value / 1000:.2f} kW" if abs(value) >= 1000 else f"{value} W"


def kwh(value: float) -> str:
    """Format an energy value with one decimal place."""
    return f"{value:.1f} kWh"


def display_fields(r: Reading) -> dict[str, str]:
    """Short display strings for a dashboard status file (keys are a stable public interface)."""
    idle_below = 20  # W; ignore the few watts a battery or meter reports when nothing is happening
    if abs(r.battery_w) < idle_below:
        battery_now = "idle"
    else:
        battery_now = f"{'in' if r.battery_w > 0 else 'out'} {watts(abs(r.battery_w))}"
    if abs(r.grid_w) < idle_below:
        grid_dir = "idle"
    else:
        grid_dir = "exporting" if r.grid_w > 0 else "importing"
    return {
        "pv_w": watts(r.pv_w),
        "pv_today": kwh(r.pv_today_kwh),
        "pv_month": f"{r.pv_month_kwh} kWh",
        "pv_total": f"{r.pv_total_kwh / 1000:.1f} MWh",
        "batt_soc": f"{r.soc_pct}%",
        "batt_soh": f"{r.soh_pct}%",
        "batt_now": battery_now,
        "batt_in_today": kwh(r.battery_in_today_kwh),
        "batt_out_today": kwh(r.battery_out_today_kwh),
        "grid_dir": grid_dir,
        "grid_power": watts(abs(r.grid_w)),
        "grid_in_today": kwh(r.grid_in_today_kwh),
        "grid_out_today": kwh(r.grid_out_today_kwh),
        "grid_v": f"{r.grid_v:.1f} V",
        "load_w": watts(r.load_w),
        "load_today": kwh(r.load_today_kwh),
        "inv_temp": f"{r.inverter_temp_c:.1f} °C",
        "faults": ", ".join(f"{code:#06x}" for code in r.fault_codes) or "none",
    }


def summary(r: Reading) -> str:
    """One-line description used in notifications and uptime-monitor messages."""
    d = display_fields(r)
    text = f"PV {d['pv_w']}, battery {d['batt_soc']} {d['batt_now']}, grid {d['grid_dir']} {d['grid_power']}"
    return text + (f", FAULT {d['faults']}" if r.has_fault else "")
