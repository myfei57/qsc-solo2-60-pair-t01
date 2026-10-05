"""Alum dose model: flow, turbidity, temperature and solution strength together.

The legacy controller multiplied inlet flow by one fixed ratio, so it could
neither see a winter solubility loss (cold water needs more alum per unit
turbidity) nor a storm turbidity spike, and it ignored how strong the dosed
solution actually was.  This module fixes the calculation:

* the process demand is a *specific dose* in mg of alum per litre of water,
  taken from a turbidity break-point curve and corrected for water
  temperature;
* the mass demand (mg/min) is specific dose times inlet flow;
* the pump output (mL/min of prepared solution, then strokes/min) divides by
  the solution concentration that was last calibrated for the shift.

Units are fixed here and reused by every audit row:

===========================  ===========================================
flow                         m3/h
turbidity                    NTU
temperature                  degrees C
specific dose (K)            mg alum / L water
concentration (C)            percent by mass of alum in the solution
solution density (rho)       g / mL of prepared solution
solution output (Q_sol)      mL solution / min
pump                         strokes / min, one stroke = stroke_volume mL
===========================  ===========================================
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

MINUTES_PER_HOUR = 60.0
# 1 mg/L applied to 1 m^3/h equals 1000/60 mg/min.
M3H_TO_LPM_FACTOR = 1000.0 / MINUTES_PER_HOUR
# Solution strength per mL = rho[g/mL] * (C_pct / 100) * 1000[mg/g], so the
# mass-to-volume divisor is rho * C_pct * 10 (see solution_flow_ml_min).
STRENGTH_DIVISOR_FACTOR = 10.0


@dataclass(frozen=True)
class Point:
    """One (x, y) corner of a piecewise linear curve."""

    x: float
    y: float

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


class Curve:
    """Piecewise linear lookup with flat ends (clamp, never extrapolate)."""

    def __init__(self, points: list[Point]) -> None:
        if len(points) < 2:
            raise ValueError("curve needs at least two points")
        ordered = sorted(points, key=lambda point: point.x)
        if any(left.x == right.x for left, right in zip(ordered, ordered[1:])):
            raise ValueError("curve x values must be strictly increasing")
        self._points = ordered

    @property
    def points(self) -> list[Point]:
        return list(self._points)

    def lookup(self, x: float) -> float:
        points = self._points
        if x <= points[0].x:
            return points[0].y
        if x >= points[-1].x:
            return points[-1].y
        for left, right in zip(points, points[1:]):
            if left.x <= x <= right.x:
                ratio = (x - left.x) / (right.x - left.x)
                return left.y + ratio * (right.y - left.y)
        return points[-1].y

    def as_list(self) -> list[dict[str, float]]:
        return [point.as_dict() for point in self._points]


@dataclass(frozen=True)
class DoseInputs:
    """Everything the specific-dose calculation consumes."""

    flow_m3h: float
    turbidity_ntu: float
    temperature_c: float

    def as_dict(self) -> dict[str, float]:
        return {
            "flow_m3h": self.flow_m3h,
            "turbidity_ntu": self.turbidity_ntu,
            "temperature_c": self.temperature_c,
        }


@dataclass(frozen=True)
class Pump:
    """Metering pump geometry and safety limits."""

    stroke_volume_ml: float = 500.0
    min_strokes_per_min: float = 0.0
    max_strokes_per_min: float = 80.0
    # Largest step the stroke command may move between ticks (strokes/min).
    max_step_spm: float = 20.0
    # |delta| below this fraction of the running command is ignored (hysteresis).
    deadband_fraction: float = 0.03
    # Below this fraction of max stroke the pump is simply switched off.
    cutoff_fraction: float = 0.02

    def __post_init__(self) -> None:
        if self.stroke_volume_ml <= 0:
            raise ValueError("stroke volume must be positive")
        if self.max_strokes_per_min <= 0:
            raise ValueError("pump range must be positive")
        if not 0.0 <= self.min_strokes_per_min < self.max_strokes_per_min:
            raise ValueError("invalid pump stroke range")
        if self.max_step_spm <= 0:
            raise ValueError("rate limit must be positive")
        if not 0.0 <= self.deadband_fraction < 1.0:
            raise ValueError("deadband fraction must lie in [0, 1)")
        if not 0.0 <= self.cutoff_fraction < 1.0:
            raise ValueError("cutoff fraction must lie in [0, 1)")

    def strokes_for_flow(self, solution_ml_min: float) -> float:
        """Convert solution output into a clamped stroke command."""

        raw = solution_ml_min / self.stroke_volume_ml
        if raw < self.cutoff_fraction * self.max_strokes_per_min:
            return 0.0
        return min(self.max_strokes_per_min, max(self.min_strokes_per_min, raw))

    def apply_limits(self, target_spm: float, previous_spm: float) -> tuple[float, bool]:
        """Return (command, rate_limited) after deadband and rate limiting."""

        target_spm = min(self.max_strokes_per_min, max(0.0, target_spm))
        delta = target_spm - previous_spm
        if abs(delta) <= self.deadband_fraction * max(previous_spm, 1e-9):
            return previous_spm, False
        step = max(-self.max_step_spm, min(self.max_step_spm, delta))
        limited = abs(step) < abs(delta) - 1e-9
        return (
            min(self.max_strokes_per_min, max(0.0, previous_spm + step)),
            limited,
        )

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def default_turbidity_curve() -> Curve:
    """Specific-dose break-points (mg/L) versus turbidity (NTU).

    Rough jar-test shape: a base dose even on clear water, then a steep limb
    through storm loads.  Tune at commissioning; the curve is persisted as
    configuration and never hard coded inside a decision.
    """

    return Curve(
        [
            Point(0.0, 10.0),
            Point(5.0, 14.0),
            Point(20.0, 25.0),
            Point(100.0, 45.0),
            Point(500.0, 70.0),
            Point(2000.0, 90.0),
        ]
    )


def default_temperature_curve() -> Curve:
    """Temperature multiplier on the specific dose.

    Cold water flocculates more slowly, so winter runs add up to +20%; the
    multiplier is 1.0 from 20 C upward.
    """

    return Curve(
        [
            Point(0.0, 1.20),
            Point(5.0, 1.18),
            Point(10.0, 1.10),
            Point(15.0, 1.03),
            Point(20.0, 1.00),
            Point(35.0, 1.00),
        ]
    )


@dataclass
class AlumConfig:
    """All tunable numbers behind an alum line, serialisable into the store."""

    # Tick cadence; ticks are aligned to whole multiples of this many seconds.
    tick_seconds: float = 60.0
    # AUTO decisions use the trailing mean of this many aligned ticks.
    auto_window_ticks: int = 5
    # Gaps between ticks longer than this (missed ticks, downtime) are not
    # integrated into delivered volume.
    max_integrate_seconds: float = 300.0
    # Specific-dose curves.
    turbidity_curve: list[Point] = field(default_factory=lambda: default_turbidity_curve().points)
    temperature_curve: list[Point] = field(
        default_factory=lambda: default_temperature_curve().points
    )
    # Prepared-solution physical constants.
    solution_density_g_ml: float = 1.12
    # Shift re-calibration schedule.
    shift_hours: int = 8
    required_readings: int = 3
    concentration_min_pct: float = 2.0
    concentration_max_pct: float = 25.0
    staleness_seconds: float = 10 * 60 * 60
    max_shift_seconds: float = 10 * 60 * 60
    # Fail-safe bootstrap / STALE floor concentration.
    floor_concentration_pct: float = 8.0
    stale_uplift: float = 1.10
    # Pump.
    pump: Pump = field(default_factory=Pump)
    # GRAB mode triggers and exit.
    grab_enter_ntu: float = 200.0
    grab_rate_ntu_per_min: float = 20.0
    grab_exit_ntu: float = 100.0
    grab_exit_ticks: int = 3
    # Which concentration basis a PENDING calibration uses while GRAB.
    grab_pending_use_old: bool = True
    # When a sensor value cannot be trusted the controller holds the last
    # command; longer than this it falls back to the floor concentration.
    sensor_hold_seconds: float = 15 * 60.0

    def turbidity_lookup(self) -> Curve:
        return Curve(list(self.turbidity_curve))

    def temperature_lookup(self) -> Curve:
        return Curve(list(self.temperature_curve))

    def validate(self) -> None:
        if self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if self.auto_window_ticks < 1:
            raise ValueError("auto_window_ticks must be positive")
        if self.max_integrate_seconds < self.tick_seconds:
            raise ValueError("max_integrate_seconds must cover at least one tick")
        Curve(list(self.turbidity_curve))
        Curve(list(self.temperature_curve))
        if self.solution_density_g_ml <= 0:
            raise ValueError("solution density must be positive")
        if self.shift_hours < 1 or self.shift_hours > 24:
            raise ValueError("shift_hours must lie in 1..24")
        if self.required_readings < 1:
            raise ValueError("required_readings must be positive")
        if not 0.0 < self.concentration_min_pct < self.concentration_max_pct:
            raise ValueError("invalid concentration sanity band")
        if self.staleness_seconds <= 0 or self.max_shift_seconds <= 0:
            raise ValueError("staleness limits must be positive")
        if not self.concentration_min_pct <= self.floor_concentration_pct <= self.concentration_max_pct:
            raise ValueError("floor concentration must lie inside the sanity band")
        if self.stale_uplift < 1.0:
            raise ValueError("stale uplift must be at least 1.0")
        if self.grab_enter_ntu <= 0 or self.grab_exit_ntu <= 0:
            raise ValueError("grab turbidity thresholds must be positive")
        if self.grab_exit_ntu >= self.grab_enter_ntu:
            raise ValueError("grab exit threshold must be below enter threshold")
        if self.grab_exit_ticks < 1:
            raise ValueError("grab_exit_ticks must be positive")
        if self.grab_rate_ntu_per_min <= 0:
            raise ValueError("grab rate-of-rise trigger must be positive")
        if self.sensor_hold_seconds <= 0:
            raise ValueError("sensor hold window must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "tick_seconds": self.tick_seconds,
            "auto_window_ticks": self.auto_window_ticks,
            "max_integrate_seconds": self.max_integrate_seconds,
            "turbidity_curve": [point.as_dict() for point in self.turbidity_curve],
            "temperature_curve": [point.as_dict() for point in self.temperature_curve],
            "solution_density_g_ml": self.solution_density_g_ml,
            "shift_hours": self.shift_hours,
            "required_readings": self.required_readings,
            "concentration_min_pct": self.concentration_min_pct,
            "concentration_max_pct": self.concentration_max_pct,
            "staleness_seconds": self.staleness_seconds,
            "max_shift_seconds": self.max_shift_seconds,
            "floor_concentration_pct": self.floor_concentration_pct,
            "stale_uplift": self.stale_uplift,
            "pump": self.pump.as_dict(),
            "grab_enter_ntu": self.grab_enter_ntu,
            "grab_rate_ntu_per_min": self.grab_rate_ntu_per_min,
            "grab_exit_ntu": self.grab_exit_ntu,
            "grab_exit_ticks": self.grab_exit_ticks,
            "grab_pending_use_old": self.grab_pending_use_old,
            "sensor_hold_seconds": self.sensor_hold_seconds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AlumConfig":
        pump_payload = payload.get("pump", {})
        pump = Pump(
            **{
                key: float(value)  # type: ignore[arg-type]
                for key, value in dict(pump_payload).items()
            }
        )
        curves = {
            name: [
                Point(x=float(item["x"]), y=float(item["y"]))  # type: ignore[index]
                for item in payload.get(name, [])  # type: ignore[arg-type]
            ]
            for name in ("turbidity_curve", "temperature_curve")
        }
        int_fields = {
            "auto_window_ticks",
            "shift_hours",
            "required_readings",
            "grab_exit_ticks",
        }
        curve_fields = set(curves)
        scalars = {}
        for key, value in payload.items():
            if key in int_fields:
                scalars[key] = int(value)  # type: ignore[literal-required]
            elif key in cls.__dataclass_fields__ and key not in curve_fields and key != "pump":
                scalars[key] = float(value)  # type: ignore[literal-required]
        config = cls(pump=pump, **curves, **scalars)  # type: ignore[arg-type]
        config.validate()
        return config

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "AlumConfig":
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("alum config must be a JSON object")
        return cls.from_dict(parsed)


def specific_dose_mg_l(inputs: DoseInputs, config: AlumConfig) -> tuple[float, float, float]:
    """Return (specific_dose, turbidity_base, temperature_factor)."""

    turbidity_base = config.turbidity_lookup().lookup(max(0.0, inputs.turbidity_ntu))
    temperature_factor = config.temperature_lookup().lookup(inputs.temperature_c)
    return turbidity_base * temperature_factor, turbidity_base, temperature_factor


def mass_rate_mg_min(specific_dose_mg_l: float, flow_m3h: float) -> float:
    """Mass of alum demanded per minute at the current inlet flow."""

    return specific_dose_mg_l * flow_m3h * M3H_TO_LPM_FACTOR


def solution_flow_ml_min(mass_mg_min: float, concentration_pct: float, density_g_ml: float) -> float:
    """Convert an alum mass demand into prepared-solution output."""

    if concentration_pct <= 0:
        raise ValueError("concentration must be positive")
    if density_g_ml <= 0:
        raise ValueError("density must be positive")
    return mass_mg_min / (density_g_ml * concentration_pct * STRENGTH_DIVISOR_FACTOR)


def solve_dose(inputs: DoseInputs, concentration_pct: float, config: AlumConfig) -> dict[str, float]:
    """Run the full chain from process inputs to pump-independent solution flow."""

    specific, turbidity_base, temperature_factor = specific_dose_mg_l(inputs, config)
    mass = mass_rate_mg_min(specific, inputs.flow_m3h)
    solution = solution_flow_ml_min(mass, concentration_pct, config.solution_density_g_ml)
    return {
        "specific_dose_mg_l": specific,
        "turbidity_base_mg_l": turbidity_base,
        "temperature_factor": temperature_factor,
        "mass_mg_min": mass,
        "solution_ml_min": solution,
    }
