"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]
    if rt.calibration.current() <= 0:
        checks.append({"name": "calibration", "status": "fail", "detail": "factor must be positive"})
    else:
        checks.append({"name": "calibration", "status": "ok", "detail": "factor set"})
    if rt.well.residual_target() <= 0:
        checks.append({"name": "residual", "status": "fail", "detail": "target must be positive"})
    else:
        checks.append({"name": "residual", "status": "ok", "detail": "target set"})
    if rt.accumulator.value() < 0:
        checks.append({"name": "quota", "status": "fail", "detail": "accumulator is negative"})
    else:
        checks.append({"name": "quota", "status": "ok", "detail": "accumulator set"})
    if rt.flow_repository.load_flow()[1]:
        checks.append({"name": "intake", "status": "ok", "detail": "flow reading present"})
    else:
        checks.append({"name": "intake", "status": "warn", "detail": "no flow reading yet"})
    if rt.stabilizer.is_stable():
        checks.append({"name": "ph", "status": "ok", "detail": "ph in the stable band"})
    else:
        checks.append({"name": "ph", "status": "warn", "detail": "ph outside the stable band"})
    alerts = rt.inventory.needs_reorder()
    if alerts:
        checks.append(
            {"name": "inventory", "status": "warn", "detail": "reorder " + ", ".join(alerts)}
        )
    else:
        checks.append({"name": "inventory", "status": "ok", "detail": "stock above reorder level"})
    view = rt.alum_engine.calibration.situation()
    if view.situation.value == "STALE":
        checks.append(
            {"name": "alum_concentration", "status": "warn",
             "detail": f"strength stale, floor {view.concentration_pct:.2f}% in force"}
        )
    elif view.situation.value == "NONE":
        checks.append(
            {"name": "alum_concentration", "status": "fail",
             "detail": "no accepted calibration, floor strength in force"}
        )
    else:
        checks.append(
            {"name": "alum_concentration", "status": "ok",
             "detail": f"{view.situation.value} at {view.concentration_pct:.2f}%"}
        )
    verification = rt.alum_engine.trail.verify()
    if verification.ok:
        checks.append(
            {"name": "alum_audit", "status": "ok", "detail": f"chain intact rows={verification.rows}"}
        )
    else:
        checks.append(
            {"name": "alum_audit", "status": "fail",
             "detail": f"broken at {verification.broken_at_seq}: {verification.reason}"}
        )
    return checks
