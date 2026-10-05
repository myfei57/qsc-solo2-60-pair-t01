"""HTTP handlers for the shift-calibrated alum dosing line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.coag import save_config

from .alum import alum_dashboard, optional_float
from .http import Request, Response, json_response, text_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def alum_tick(server: "Server", request: Request) -> Response:
    """Run one aligned dosing tick from the current sensor values."""

    epoch = request.int_field("epoch") if "epoch" in request.payload else None
    decision = server.runtime.alum_engine.tick(
        flow_m3h=optional_float(request, "flow"),
        turbidity_ntu=optional_float(request, "turbidity"),
        temperature_c=optional_float(request, "temperature"),
        epoch=None if epoch in (None, 0) else float(epoch),
    )
    return json_response(decision)


def alum_last(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.last_decision() or {})


def alum_panel(server: "Server", request: Request) -> Response:
    return text_response(alum_dashboard(server.runtime))


def alum_grab_arm(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.arm_grab())


def alum_grab_release(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.release_grab())


def alum_mode(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.mode_state())


def _optional_epoch(request: Request) -> float | None:
    if "epoch" not in request.payload:
        return None
    epoch = request.int_field("epoch")
    return None if epoch == 0 else float(epoch)


def alum_tickets_open(server: "Server", request: Request) -> Response:
    ticket, events = server.runtime.alum_engine.calibration.open_ticket(
        epoch=_optional_epoch(request)
    )
    return json_response({"ticket": ticket.as_dict(), "events": [_event_payload(e) for e in events]})


def alum_tickets(server: "Server", request: Request) -> Response:
    return json_response(
        {"tickets": [ticket.as_dict() for ticket in server.runtime.alum_engine.calibration.tickets()]}
    )


def alum_ticket_get(server: "Server", request: Request) -> Response:
    ticket = server.runtime.alum_engine.calibration.ticket(request.str_field("ticket_id"))
    if ticket is None:
        return json_response({"ok": False, "error": "unknown ticket"}, status=404)
    return json_response({"ticket": ticket.as_dict(), "ok": True})


def alum_reading(server: "Server", request: Request) -> Response:
    replicate = request.int_field("replicate")
    value = request.float_field("value_pct")
    ticket, events = server.runtime.alum_engine.calibration.submit_reading(
        request.str_field("ticket_id"), replicate, value, epoch=_optional_epoch(request)
    )
    return json_response({"ticket": ticket.as_dict(), "events": [_event_payload(e) for e in events]})


def alum_reading_lost(server: "Server", request: Request) -> Response:
    ticket, events = server.runtime.alum_engine.calibration.report_lost_reading(
        request.str_field("ticket_id"), request.int_field("replicate"),
        epoch=_optional_epoch(request),
    )
    return json_response({"ticket": ticket.as_dict(), "events": [_event_payload(e) for e in events]})


def alum_recover(server: "Server", request: Request) -> Response:
    """Recovery endpoint; safe to call repeatedly (idempotent)."""

    ticket, events = server.runtime.alum_engine.calibration.recover(
        request.str_field("ticket_id"), epoch=_optional_epoch(request)
    )
    return json_response({"ticket": ticket.as_dict(), "events": [_event_payload(e) for e in events]})


def alum_reject(server: "Server", request: Request) -> Response:
    ticket, events = server.runtime.alum_engine.calibration.reject(
        request.str_field("ticket_id"), request.str_field("reason", "manual reject"),
        epoch=_optional_epoch(request),
    )
    return json_response({"ticket": ticket.as_dict(), "events": [_event_payload(e) for e in events]})


def alum_concentration(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.calibration.situation().as_dict())


def alum_versions(server: "Server", request: Request) -> Response:
    return json_response(
        {"versions": [v.as_dict() for v in server.runtime.alum_engine.calibration.versions()]}
    )


def alum_audit(server: "Server", request: Request) -> Response:
    trail = server.runtime.alum_engine.trail
    rows = trail.rows()
    limit = request.int_field("limit", 50)
    if limit <= 0:
        limit = 50
    selected = rows[-limit:]
    return json_response(
        {
            "rows": [row.as_dict() for row in selected],
            "count": len(rows),
            "verification": trail.verify().as_dict(),
        }
    )


def alum_audit_verify(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.trail.verify().as_dict())


def alum_state(server: "Server", request: Request) -> Response:
    engine = server.runtime.alum_engine
    return json_response(
        {
            "mode": engine.mode_state(),
            "concentration": engine.calibration.situation().as_dict(),
            "last_decision": engine.last_decision(),
            "totals": engine.totals(),
            "chain": engine.trail.verify().as_dict(),
            "config": engine.config.as_dict(),
        }
    )


def alum_config_get(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.alum_engine.config.as_dict())


def alum_config_put(server: "Server", request: Request) -> Response:
    current = server.runtime.alum_engine.config
    merged = current.as_dict()
    for key, value in request.payload.items():
        if key in merged:
            merged[key] = value
    from waterplant.coag import AlumConfig

    try:
        updated = AlumConfig.from_dict(merged)
    except (KeyError, TypeError, ValueError) as exc:
        return json_response({"ok": False, "error": str(exc)}, status=400)
    save_config(server.runtime.store, updated)
    server.runtime.reset_alum_engine()
    return json_response({"ok": True, "config": updated.as_dict()})


def _event_payload(event: object) -> dict[str, object]:
    return dict(event)  # type: ignore[arg-type]
