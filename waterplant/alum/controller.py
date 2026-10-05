"""Alum dosing engine: window means in, one audited decision out.

Two rules are fixed by design:
  * the dose always follows the trailing window mean of flow and turbidity,
    never a single instantaneous reading;
  * while a recalibration is unconfirmed, normal dosing waits for the new
    concentration and only an emergency turbidity mean may dose on the
    previous (stale) strength.
"""

from __future__ import annotations

import json

from waterplant.audit import Auditor
from waterplant.intake import validate_flow
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .concentration import ConcentrationLedger
from .model import (
    DEFAULT_TEMP_C,
    EMERGENCY_TURBIDITY,
    pure_dose_mgl,
    solution_rate_lph,
    temperature_factor,
    validate_temperature,
    validate_turbidity,
)
from .report import AlumState, DoseDecision

FLOW_WINDOW_KEY = "alum:window:flow"
TURBIDITY_WINDOW_KEY = "alum:window:turbidity"
WINDOW_SIZE_KEY = "alum:window:size"
TEMPERATURE_KEY = "alum:temperature"
DECISION_KEY = "alum:last_decision"

DEFAULT_WINDOW = 6
MAX_WINDOW = 60
BASIS = "window-mean"

STATUS_DOSED = "dosed"
STATUS_HELD = "held"
REASON_NORMAL = "normal"
REASON_EMERGENCY_STALE = "emergency_stale"
REASON_AWAITING_CONFIRMATION = "awaiting_confirmation"
REASON_NO_READINGS = "no_readings"

AUDIT_KIND = "alum"


def _round4(value: float) -> float:
    return round(float(value), 4)


class AlumController:
    """Computes the alum pump rate from flow, turbidity, temperature and strength."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.ledger = ConcentrationLedger(store)
        self._auditor = Auditor(store)

    def ensure_defaults(self) -> None:
        """Seed the concentration snapshot and the averaging window."""

        self.ledger.ensure_default()
        if not self._store.get(WINDOW_SIZE_KEY)[1]:
            save_float(self._store, WINDOW_SIZE_KEY, float(DEFAULT_WINDOW))

    def window(self) -> int:
        value, present = load_float(self._store, WINDOW_SIZE_KEY)
        if not present or value <= 0:
            return DEFAULT_WINDOW
        return min(int(value), MAX_WINDOW)

    def set_window(self, value: int) -> int:
        if value <= 0:
            raise ValueError("window must be positive")
        if value > MAX_WINDOW:
            raise ValueError("window exceeds the supported range")
        save_float(self._store, WINDOW_SIZE_KEY, float(value))
        self._store.put(FLOW_WINDOW_KEY, json.dumps(self._window_values(FLOW_WINDOW_KEY)[-value:]))
        self._store.put(
            TURBIDITY_WINDOW_KEY, json.dumps(self._window_values(TURBIDITY_WINDOW_KEY)[-value:])
        )
        return value

    def temperature(self) -> float:
        value, present = load_float(self._store, TEMPERATURE_KEY)
        return value if present else DEFAULT_TEMP_C

    def _window_values(self, key: str) -> list[float]:
        raw, present = self._store.get(key)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        values: list[float] = []
        for item in parsed:
            try:
                values.append(float(item))
            except (TypeError, ValueError):
                continue
        return values

    def _append(self, key: str, value: float) -> None:
        values = self._window_values(key)
        values.append(float(value))
        self._store.put(key, json.dumps(values[-self.window() :]))

    def _means(self) -> tuple[float, float, int]:
        flows = self._window_values(FLOW_WINDOW_KEY)
        turbidities = self._window_values(TURBIDITY_WINDOW_KEY)
        samples = min(len(flows), len(turbidities))
        if samples == 0:
            return 0.0, 0.0, 0
        return sum(flows) / len(flows), sum(turbidities) / len(turbidities), samples

    def record_reading(self, flow: float, turbidity: float, temperature: float | None) -> None:
        """Persist one observation before anything downstream consumes it."""

        self._append(FLOW_WINDOW_KEY, flow)
        self._append(TURBIDITY_WINDOW_KEY, turbidity)
        if temperature is not None:
            save_float(self._store, TEMPERATURE_KEY, temperature)

    def dose(self, flow: float, turbidity: float, temperature: float | None = None) -> DoseDecision:
        """Record one reading and decide the pump rate against the fresh basis."""

        validate_flow(flow)
        validate_turbidity(turbidity)
        if temperature is not None:
            validate_temperature(temperature)
        self.record_reading(flow, turbidity, temperature)
        return self.decide()

    def decide(self) -> DoseDecision:
        """Decide the pump rate from the current windows and concentration.

        The same record is returned to the caller, persisted for the console
        and appended to the audit trail, so all three always agree.
        """

        state = self.ledger.current()
        flow_mean, turbidity_mean, samples = self._means()
        temp_c = self.temperature()
        factor = temperature_factor(temp_c)
        status = STATUS_DOSED
        reason = REASON_NORMAL
        rate = 0.0
        pure = 0.0
        if samples == 0:
            status, reason = STATUS_HELD, REASON_NO_READINGS
        elif state.pending and turbidity_mean >= EMERGENCY_TURBIDITY:
            status, reason = STATUS_DOSED, REASON_EMERGENCY_STALE
        elif state.pending:
            status, reason = STATUS_HELD, REASON_AWAITING_CONFIRMATION
        if status == STATUS_DOSED:
            pure = pure_dose_mgl(turbidity_mean, temp_c)
            rate = solution_rate_lph(pure, flow_mean, state.value)
        decision = DoseDecision(
            status=status,
            reason=reason,
            basis=BASIS,
            solution_rate_lph=_round4(rate),
            pure_dose_mgl=_round4(pure),
            flow_mean=_round4(flow_mean),
            turbidity_mean=_round4(turbidity_mean),
            temperature_c=temp_c,
            temp_factor=_round4(factor),
            concentration=state.value,
            previous_concentration=state.previous,
            epoch=state.epoch,
            shift=state.shift,
            window=self.window(),
            samples=samples,
        )
        payload = json.dumps(decision.as_dict(), ensure_ascii=False)
        self._store.put(DECISION_KEY, payload)
        self._auditor.record(AUDIT_KIND, payload)
        return decision

    def last_decision(self) -> dict[str, object] | None:
        raw, present = self._store.get(DECISION_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def state(self) -> AlumState:
        concentration = self.ledger.current()
        flow_mean, turbidity_mean, samples = self._means()
        return AlumState(
            concentration=concentration.value,
            previous_concentration=concentration.previous,
            epoch=concentration.epoch,
            shift=concentration.shift,
            pending=concentration.pending,
            pending_shift=concentration.pending_shift,
            window=self.window(),
            samples=samples,
            flow_mean=_round4(flow_mean),
            turbidity_mean=_round4(turbidity_mean),
            temperature_c=self.temperature(),
            last_decision=self.last_decision(),
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"alum concentration={state.concentration:.4f} epoch={state.epoch} "
            f"pending={state.pending} samples={state.samples}"
        )
