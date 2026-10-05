"""Console helpers specific to the alum dosing line."""

from __future__ import annotations

from .http import Request, RequestError
from .runtime import Runtime


def optional_float(request: Request, name: str) -> float | None:
    """Read a number field where an absent/null value means "sensor lost"."""

    if name not in request.payload:
        return None
    value = request.payload[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RequestError(400, f"field {name} must be a number")
    try:
        return float(value)
    except ValueError as exc:
        raise RequestError(400, f"field {name} must be a number") from exc


def alum_dashboard(rt: Runtime) -> str:
    """Operator text panel: mode, strength basis, latest dose and chain state."""

    engine = rt.alum_engine
    decision = engine.last_decision()
    view = engine.calibration.situation()
    chain = engine.trail.verify()
    lines = [
        "alum dosing panel",
        (
            f"mode={engine.mode_state()['mode']} situation={view.situation.value} "
            f"basis_in_force={view.concentration_pct:.3f}% "
            f"version={view.version.id if view.version else '-'} "
            f"age_s={view.age_seconds:.0f}"
        ),
        f"chain ok={chain.ok} rows={chain.rows}" + (
            "" if chain.ok else f" broken_at={chain.broken_at_seq} {chain.reason}"
        ),
    ]
    ticket = view.active_ticket
    if ticket is not None:
        lines.append(
            f"shift ticket={ticket.id} shift={ticket.shift_id} status={ticket.status} "
            f"readings={len(ticket.readings)}/{engine.config.required_readings} "
            f"lost={ticket.lost_replicates} recoveries={ticket.recovery_count}"
        )
    else:
        lines.append("shift ticket=none (current shift calibrated)")
    if decision is None:
        lines.append("no decision tick yet")
    else:
        concentration = decision["concentration"]
        inputs = decision["inputs"]
        dose = decision["dose"]
        pump = decision["pump"]
        accounting = decision["accounting"]
        lines.append(
            f"last tick={decision['at']} mode={decision['mode']} basis={decision['dose_basis']}"
        )
        lines.append(
            f"  flow={inputs['flow_m3h']} m3/h turbidity={inputs['turbidity_ntu']} NTU "
            f"temp={inputs['temperature_c']} C"
        )
        lines.append(
            f"  K={dose['specific_dose_mg_l']:.3f} mg/L "
            f"(turb_base={dose['turbidity_base_mg_l']:.3f} "
            f"temp_factor={dose['temperature_factor']:.3f})"
        )
        lines.append(
            f"  concentration used={concentration['used_pct']:.3f}% "
            f"prior={concentration['previous_pct']} uplift={concentration['uplift']:.2f}"
        )
        lines.append(
            f"  solution={dose['solution_ml_min']:.1f} mL/min "
            f"pump target={pump['target_spm']:.3f} command={pump['command_spm']:.3f} spm "
            f"frozen={pump['frozen']} shutdown={pump['safe_shutdown']}"
        )
        lines.append(
            f"  delivered last interval={accounting['delivered_ml']:.0f} mL "
            f"shift-day total={accounting['day_total_ml']:.0f} mL "
            f"cumulative={accounting['cumulative_ml']:.0f} mL"
        )
        lines.append(f"  reasons={', '.join(decision['reasons'])}")
    return "\n".join(lines) + "\n"
