"""Coagulant dosing: legacy ratio doser and the shift-calibrated alum line."""

from .clock import Clock, FakeClock, SystemClock, to_iso
from .concentration import (
    CalibTicket,
    CalibrationService,
    ConcentrationVersion,
    ConcentrationView,
    Reading,
    Situation,
    TicketStatus,
    shift_start_for,
)
from .doser import COAGULANT, Doser
from .engine import (
    AlumEngine,
    DoseBasis,
    Mode,
    TickInput,
    load_config,
    save_config,
    slot_for,
)
from .model import (
    AlumConfig,
    Curve,
    DoseInputs,
    Point,
    Pump,
    mass_rate_mg_min,
    solution_flow_ml_min,
    specific_dose_mg_l,
)
from . import engine as _engine
from .report import DoseState
from .trail import AuditRow, AuditTrail, ChainVerification, TRAIL_KEY

REASONS = tuple(
    value
    for name, value in vars(_engine).items()
    if name.startswith("REASON_") and isinstance(value, str)
)

__all__ = [
    "COAGULANT",
    "AlumConfig",
    "AlumEngine",
    "AuditRow",
    "AuditTrail",
    "CalibTicket",
    "CalibrationService",
    "ChainVerification",
    "Clock",
    "ConcentrationVersion",
    "ConcentrationView",
    "Curve",
    "DoseBasis",
    "DoseInputs",
    "DoseState",
    "Doser",
    "FakeClock",
    "Mode",
    "Point",
    "Pump",
    "Reading",
    "REASONS",
    "Situation",
    "SystemClock",
    "TRAIL_KEY",
    "TickInput",
    "TicketStatus",
    "load_config",
    "mass_rate_mg_min",
    "save_config",
    "shift_start_for",
    "slot_for",
    "solution_flow_ml_min",
    "specific_dose_mg_l",
    "to_iso",
]
