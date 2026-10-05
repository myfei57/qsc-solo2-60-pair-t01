"""Alum dosing projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConcentrationState:
    """The confirmed solution strength and any pending recalibration."""

    value: float
    previous: float
    epoch: int
    shift: str
    pending: bool
    pending_shift: str

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "previous": self.previous,
            "epoch": self.epoch,
            "shift": self.shift,
            "pending": self.pending,
            "pending_shift": self.pending_shift,
        }


@dataclass(frozen=True)
class CalibrationOutcome:
    """The result of a per-shift recalibration request."""

    status: str
    shift: str
    value: float
    previous: float
    epoch: int
    calibration_id: str
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "shift": self.shift,
            "value": self.value,
            "previous": self.previous,
            "epoch": self.epoch,
            "calibration_id": self.calibration_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class RecoveryOutcome:
    """The result of recovering a calibration whose reading was lost."""

    recovered: bool
    changed: bool
    reason: str
    value: float
    source: str
    epoch: int
    calibration_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "recovered": self.recovered,
            "changed": self.changed,
            "reason": self.reason,
            "value": self.value,
            "source": self.source,
            "epoch": self.epoch,
            "calibration_id": self.calibration_id,
        }


@dataclass(frozen=True)
class DoseDecision:
    """One dosing decision: the record that is returned, persisted and audited."""

    status: str
    reason: str
    basis: str
    solution_rate_lph: float
    pure_dose_mgl: float
    flow_mean: float
    turbidity_mean: float
    temperature_c: float
    temp_factor: float
    concentration: float
    previous_concentration: float
    epoch: int
    shift: str
    window: int
    samples: int

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "basis": self.basis,
            "solution_rate_lph": self.solution_rate_lph,
            "pure_dose_mgl": self.pure_dose_mgl,
            "flow_mean": self.flow_mean,
            "turbidity_mean": self.turbidity_mean,
            "temperature_c": self.temperature_c,
            "temp_factor": self.temp_factor,
            "concentration": self.concentration,
            "previous_concentration": self.previous_concentration,
            "epoch": self.epoch,
            "shift": self.shift,
            "window": self.window,
            "samples": self.samples,
        }


@dataclass(frozen=True)
class AlumState:
    """Everything the console shows on the alum panel."""

    concentration: float
    previous_concentration: float
    epoch: int
    shift: str
    pending: bool
    pending_shift: str
    window: int
    samples: int
    flow_mean: float
    turbidity_mean: float
    temperature_c: float
    last_decision: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "concentration": self.concentration,
            "previous_concentration": self.previous_concentration,
            "epoch": self.epoch,
            "shift": self.shift,
            "pending": self.pending,
            "pending_shift": self.pending_shift,
            "window": self.window,
            "samples": self.samples,
            "flow_mean": self.flow_mean,
            "turbidity_mean": self.turbidity_mean,
            "temperature_c": self.temperature_c,
            "last_decision": self.last_decision,
        }
