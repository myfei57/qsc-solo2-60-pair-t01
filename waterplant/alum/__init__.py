"""Alum dosing with shift calibrated solution strength."""

from .concentration import ConcentrationLedger
from .controller import BASIS, DEFAULT_WINDOW, MAX_WINDOW, AlumController
from .model import (
    DEFAULT_CONCENTRATION,
    DEFAULT_TEMP_C,
    EMERGENCY_TURBIDITY,
    pure_dose_mgl,
    solution_rate_lph,
    temperature_factor,
    validate_concentration,
    validate_temperature,
    validate_turbidity,
)
from .report import (
    AlumState,
    CalibrationOutcome,
    ConcentrationState,
    DoseDecision,
    RecoveryOutcome,
)

__all__ = [
    "BASIS",
    "DEFAULT_CONCENTRATION",
    "DEFAULT_TEMP_C",
    "DEFAULT_WINDOW",
    "EMERGENCY_TURBIDITY",
    "MAX_WINDOW",
    "AlumController",
    "AlumState",
    "CalibrationOutcome",
    "ConcentrationLedger",
    "ConcentrationState",
    "DoseDecision",
    "RecoveryOutcome",
    "pure_dose_mgl",
    "solution_rate_lph",
    "temperature_factor",
    "validate_concentration",
    "validate_temperature",
    "validate_turbidity",
]
