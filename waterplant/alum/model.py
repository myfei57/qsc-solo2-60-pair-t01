"""Dose model constants and pure dose arithmetic."""

from __future__ import annotations

BASE_DOSE_MGL = 5.0  # base coagulant demand in mg/L of pure alum
TURBIDITY_COEFF = 0.8  # extra mg/L of pure alum per NTU
REFERENCE_TEMP_C = 20.0  # water temperature the base demand is tuned for
COLD_SLOPE = 0.03  # extra demand fraction per degree below the reference
WARM_SLOPE = 0.01  # demand reduction per degree above the reference
MIN_TEMP_FACTOR = 0.8
MAX_TEMP_FACTOR = 1.6
EMERGENCY_TURBIDITY = 50.0  # turbidity mean that forces the stale-strength override
DEFAULT_CONCENTRATION = 0.10  # alum solution mass fraction before any calibration
MAX_CONCENTRATION = 0.5
DEFAULT_TEMP_C = 20.0
MAX_TURBIDITY = 10_000.0


def temperature_factor(temp_c: float) -> float:
    """Cold water needs more coagulant, warm water slightly less."""

    if temp_c < REFERENCE_TEMP_C:
        return min(MAX_TEMP_FACTOR, 1.0 + (REFERENCE_TEMP_C - temp_c) * COLD_SLOPE)
    return max(MIN_TEMP_FACTOR, 1.0 - (temp_c - REFERENCE_TEMP_C) * WARM_SLOPE)


def pure_dose_mgl(turbidity: float, temp_c: float) -> float:
    """Pure alum demand per litre of treated water."""

    return (BASE_DOSE_MGL + turbidity * TURBIDITY_COEFF) * temperature_factor(temp_c)


def solution_rate_lph(pure_dose: float, flow_m3h: float, concentration: float) -> float:
    """Alum solution pump rate in L/h for the given flow and solution strength."""

    if concentration <= 0:
        raise ValueError("concentration must be positive")
    return pure_dose * flow_m3h / (concentration * 1000.0)


def validate_concentration(value: float) -> None:
    """Reject solution strengths the dosing skid cannot be running."""

    if value <= 0:
        raise ValueError("concentration must be positive")
    if value > MAX_CONCENTRATION:
        raise ValueError("concentration exceeds the supported range")


def validate_turbidity(value: float) -> None:
    """Reject turbidity readings outside the instrument range."""

    if value < 0:
        raise ValueError("turbidity must be non-negative")
    if value > MAX_TURBIDITY:
        raise ValueError("turbidity exceeds the supported range")


def validate_temperature(value: float) -> None:
    """Reject water temperatures outside the supported instrument range."""

    if value < -5.0:
        raise ValueError("temperature below the supported range")
    if value > 50.0:
        raise ValueError("temperature exceeds the supported range")
