"""Deterministic walk-through of the alum dosing line.

Run with ``python -m waterplant.alum_demo``. It uses a fake clock against a
temporary store and prints the same decision payloads the console exposes, so
operators can see every frozen policy without touching a live pump.
"""

from __future__ import annotations

import json
import tempfile

from waterplant.coag import (
    AlumConfig,
    AlumEngine,
    FakeClock,
    save_config,
)
from waterplant.store import Store


def _print_decision(decision: dict[str, object], label: str) -> None:
    concentration = decision["concentration"]
    inputs = decision["inputs"]
    dose = decision["dose"]
    pump = decision["pump"]
    accounting = decision["accounting"]
    print(f"-- {label} @ {decision['at']} ({decision['mode']}/{decision['dose_basis']})")
    print(
        f"   in: flow={inputs['flow_m3h']} m3/h turbidity={inputs['turbidity_ntu']} NTU "
        f"temp={inputs['temperature_c']} C | strength={concentration['used_pct']:.2f}% "
        f"({concentration['active_version_id'] or 'floor'})"
    )
    print(
        f"   K={dose['specific_dose_mg_l']:.2f} mg/L solution={dose['solution_ml_min']:.1f} mL/min "
        f"pump command={pump['command_spm']:.2f} spm frozen={pump['frozen']} "
        f"shutdown={pump['safe_shutdown']}"
    )
    print(
        f"   delivered interval={accounting['delivered_ml']:.0f} mL "
        f"day total={accounting['day_total_ml']:.0f} mL"
    )
    print("   reasons:", ", ".join(decision["reasons"]))


def run() -> None:
    tmp = tempfile.TemporaryDirectory()
    store = Store.open(f"{tmp.name}/demo.json")
    config = AlumConfig()
    config.shift_hours = 8
    config.staleness_seconds = 12 * 3600
    save_config(store, config)

    clock = FakeClock(0.0)
    engine = AlumEngine(store, config, clock)
    calibration = engine.calibration

    print("== cold winter morning: the shift calibrates a strong 10% solution ==")
    ticket, _ = calibration.open_ticket(epoch=0)
    for replicate, value in enumerate((10.0, 10.1, 9.9), start=1):
        calibration.submit_reading(ticket.id, replicate, value, epoch=replicate * 10)

    for minute in (60, 120, 180):
        _print_decision(
            engine.tick(800.0, 18.0, 4.0, epoch=float(minute)),
            f"normal tick {minute}",
        )

    print("\n== storm arrives: turbidity spike arms GRAB, instantaneous dose ==")
    _print_decision(engine.tick(900.0, 420.0, 4.0, epoch=240.0), "storm front")
    _print_decision(engine.tick(900.0, 60.0, 4.0, epoch=300.0), "spiky dip, turbidity max-held")

    print("\n== shift handover: new calibration pending while the storm continues ==")
    next_ticket, _ = calibration.open_ticket(epoch=8 * 3600)
    calibration.submit_reading(next_ticket.id, 1, 8.4, epoch=8 * 3600 + 10)
    _print_decision(
        engine.tick(900.0, 420.0, 4.0, epoch=8 * 3600 + 60),
        "PENDING strength: GRAB keeps covering with the OLD strength",
    )
    calibration.report_lost_reading(next_ticket.id, 2, epoch=8 * 3600 + 80)
    print("   replicate 2 lost -> ticket status", calibration.ticket(next_ticket.id).status)
    calibration.recover(next_ticket.id, epoch=8 * 3600 + 100)
    calibration.submit_reading(next_ticket.id, 2, 8.5, epoch=8 * 3600 + 120)
    calibration.submit_reading(next_ticket.id, 3, 8.3, epoch=8 * 3600 + 130)
    _print_decision(
        engine.tick(900.0, 420.0, 4.0, epoch=8 * 3600 + 180),
        "after ACCEPTED the NEXT tick uses the refreshed 8.4% strength",
    )

    print("\n== flow meter dies: hold, then safe shutdown ==")
    _print_decision(engine.tick(None, 420.0, 4.0, epoch=8 * 3600 + 240), "1 min blind: hold pump")
    _print_decision(
        engine.tick(None, 420.0, 4.0, epoch=8 * 3600 + 240 + 16 * 60),
        "16 min blind: pump shutdown",
    )

    print("\n== replay the same aligned tick: identical command, one audit row ==")
    replay = engine.tick(900.0, 1.0, 1.0, epoch=8 * 3600 + 240 + 16 * 60)
    print("   replay reasons:", [r for r in replay["reasons"] if "REPLAY" in r])

    print("\n== audit chain verification ==")
    verification = engine.trail.verify()
    print(" ", verification)
    print("  totals:", json.dumps(engine.totals(), sort_keys=True))
    tmp.cleanup()


if __name__ == "__main__":
    run()
