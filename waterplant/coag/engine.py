"""Alum dosing decision engine.

One call to :meth:`AlumEngine.tick` is the whole story for one aligned tick:

1. the shift calibration schedule is maintained first;
2. the concentration basis is read *as of* the tick slot, so a calibration
   accepted during the tick can only change a later tick (strict ordering);
3. AUTO/GRAB mode and the INSTANT/MEAN input basis are resolved by fixed
   policy, not operator mood;
4. demand, solution flow and pump strokes are computed;
5. the decision, the concentration basis before/after and every policy reason
   land in the hash-chained audit trail together with the pump command.

The policy for a pending calibration while storm dosing (use the old strength
to keep feeding the flash mixer, or freeze and wait for the new strength) is a
configuration decision taken per line; the choice is recorded on every row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from waterplant.store.store import Store

from .clock import Clock, SystemClock, to_iso
from .concentration import (
    CalibrationService,
    ConcentrationView,
    Situation,
)
from .model import (
    AlumConfig,
    DoseInputs,
    mass_rate_mg_min,
    solution_flow_ml_min,
    specific_dose_mg_l,
)
from .trail import AuditTrail

ENGINE_KEY = "coag:alum:engine"
CONFIG_KEY = "coag:alum:config"
DEFAULT_TEMPERATURE_C = 20.0


class Mode(str, Enum):
    AUTO = "AUTO"
    GRAB = "GRAB"


class DoseBasis(str, Enum):
    """Whether a tick used the instantaneous sample or a window mean."""

    INSTANT = "INSTANT"
    MEAN = "MEAN"


# Policy reason codes. They are part of the audit contract, so their spelling
# is fixed and they double as console alarm identifiers.
REASON_AUTO_MEAN = "AUTO_MEAN_BASIS"
REASON_GRAB_INSTANT = "GRAB_INSTANT_BASIS"
REASON_FRESH = "CONCENTRATION_FRESH"
REASON_PENDING_AUTO_OLD = "PENDING_AUTO_USES_OLD_UNTIL_CONFIRMED"
REASON_PENDING_GRAB_OLD = "GRAB_PENDING_USES_OLD_TO_COVER_SPIKE"
REASON_PENDING_GRAB_WAIT = "GRAB_PENDING_FREEZES_PUMP_AWAITING_CONFIRMATION"
REASON_STALE_FLOOR = "STRENGTH_STALE_USES_FLOOR_PLUS_UPLIFT"
REASON_NONE_FLOOR = "NO_CALIBRATION_USES_FLOOR_PLUS_UPLIFT"
REASON_GRAB_AUTO_ENTER = "GRAB_ENTERED_ON_TURBIDITY"
REASON_GRAB_RATE_ENTER = "GRAB_ENTERED_ON_RATE_OF_RISE"
REASON_GRAB_MANUAL = "GRAB_ARMED_MANUALLY"
REASON_GRAB_EXIT = "GRAB_EXITED_AFTER_LOW_TURBIDITY_RUN"
REASON_FLOW_HOLD = "FLOW_SENSOR_LOST_HOLDING_LAST_COMMAND"
REASON_FLOW_SHUT = "FLOW_SENSOR_LOST_TOO_LONG_PUMP_SHUTDOWN"
REASON_TURB_HOLD = "TURBIDITY_SENSOR_LOST_USING_LAST_KNOWN"
REASON_TURB_STALE_UPLIFT = "TURBIDITY_SENSOR_STALE_UPLIFT_APPLIED"
REASON_TURB_UNKNOWN = "TURBIDITY_SENSOR_NEVER_READ_ASSUMING_ZERO"
REASON_TEMP_DEFAULT = "TEMPERATURE_SENSOR_LOST_DEFAULT_20C"
REASON_TEMP_HOLD = "TEMPERATURE_SENSOR_LOST_USING_LAST_KNOWN"
REASON_PUMP_RATE_LIMITED = "PUMP_MOVE_RATE_LIMITED"
REASON_PUMP_DEADBAND = "PUMP_MOVE_WITHIN_DEADBAND"
REASON_PUMP_CLAMPED_MAX = "PUMP_CLAMPED_TO_MAX_STROKES"
REASON_PUMP_CUTOFF = "PUMP_BELOW_CUTOFF_SWITCHED_OFF"
REASON_TICK_REPLAY = "TICK_SLOT_REPLAY_RETURNED_STORED_DECISION"
REASON_GAP_NOT_INTEGRATED = "TICK_GAP_TOO_LONG_VOLUME_NOT_INTEGRATED"


@dataclass(frozen=True)
class TickInput:
    """Sensor observation for one tick; ``None`` means the reading is absent."""

    flow_m3h: float | None
    turbidity_ntu: float | None
    temperature_c: float | None

    @classmethod
    def from_kwargs(
        cls,
        flow_m3h: float | None,
        turbidity_ntu: float | None,
        temperature_c: float | None,
    ) -> "TickInput":
        return cls(flow_m3h, turbidity_ntu, temperature_c)


@dataclass(frozen=True)
class PolicyDecision:
    """How the (situation, mode) pair maps onto dosing actions."""

    concentration_pct: float
    basis: DoseBasis
    freeze_pump: bool
    uplift: float
    reasons: tuple[str, ...]


def load_config(store: Store) -> AlumConfig:
    raw, present = store.get(CONFIG_KEY)
    if not present:
        return AlumConfig()
    return AlumConfig.from_json(raw)


def save_config(store: Store, config: AlumConfig) -> None:
    config.validate()
    store.put(CONFIG_KEY, config.to_json())


def slot_for(epoch: float, tick_seconds: float) -> float:
    return (epoch // tick_seconds) * tick_seconds


def _policy_for(
    view: ConcentrationView,
    mode: Mode,
    config: AlumConfig,
    has_previous_command: bool,
) -> PolicyDecision:
    """Resolve the fixed policy table for one tick."""

    if view.situation == Situation.FRESH:
        if mode == Mode.GRAB:
            return PolicyDecision(
                view.concentration_pct, DoseBasis.INSTANT, False, 1.0,
                (REASON_FRESH, REASON_GRAB_INSTANT),
            )
        return PolicyDecision(
            view.concentration_pct, DoseBasis.MEAN, False, 1.0,
            (REASON_FRESH, REASON_AUTO_MEAN),
        )

    if view.situation == Situation.PENDING:
        old_strength = view.version.concentration_pct if view.version is not None else view.concentration_pct
        if mode == Mode.AUTO:
            return PolicyDecision(
                old_strength, DoseBasis.MEAN, False, 1.0,
                tuple(view.reasons) + (REASON_PENDING_AUTO_OLD,),
            )
        if config.grab_pending_use_old:
            return PolicyDecision(
                old_strength, DoseBasis.INSTANT, False, 1.0,
                tuple(view.reasons) + (REASON_PENDING_GRAB_OLD, REASON_GRAB_INSTANT),
            )
        # Storm cover is configured as "wait for confirmation": freeze the
        # running pump. With no running command yet, fall back to the old
        # strength rather than dosing nothing into a storm.
        if has_previous_command:
            return PolicyDecision(
                old_strength, DoseBasis.INSTANT, True, 1.0,
                tuple(view.reasons) + (REASON_PENDING_GRAB_WAIT, REASON_GRAB_INSTANT),
            )
        return PolicyDecision(
            old_strength, DoseBasis.INSTANT, False, 1.0,
            tuple(view.reasons) + (REASON_PENDING_GRAB_WAIT, REASON_PENDING_GRAB_OLD),
        )

    if view.situation == Situation.STALE:
        reason = REASON_STALE_FLOOR
    else:
        reason = REASON_NONE_FLOOR
    basis = DoseBasis.INSTANT if mode == Mode.GRAB else DoseBasis.MEAN
    basis_reason = REASON_GRAB_INSTANT if mode == Mode.GRAB else REASON_AUTO_MEAN
    return PolicyDecision(
        view.concentration_pct, basis, False, config.stale_uplift,
        tuple(view.reasons) + (reason, basis_reason),
    )


class AlumEngine:
    """Drives one alum line and persists every decision."""

    def __init__(
        self,
        store: Store,
        config: AlumConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._config = config or load_config(store)
        self._trail = AuditTrail(store)
        self._calibration = CalibrationService(
            store, self._config, self._clock, audit_sink=self._trail
        )

    @property
    def calibration(self) -> CalibrationService:
        return self._calibration

    @property
    def trail(self) -> AuditTrail:
        return self._trail

    @property
    def config(self) -> AlumConfig:
        return self._config

    # -- state -------------------------------------------------------------

    def _blank_state(self) -> dict[str, object]:
        return {
            "last_slot": None,
            "last_command_spm": 0.0,
            "last_known": {},
            "window": [],
            "mode": Mode.AUTO.value,
            "manual_grab": False,
            "grab_arm_seq": 0,
            "exit_run": 0,
            "grab_since_slot": None,
            "grab_max_turbidity": None,
            "cumulative_ml": 0.0,
            "totals_by_date": {},
            "last_seq": 0,
            "last_decision": None,
        }

    def _load_state(self) -> dict[str, object]:
        raw, present = self._store.get(ENGINE_KEY)
        if not present:
            return self._blank_state()
        try:
            parsed = json.loads(raw)
        except ValueError:
            return self._blank_state()
        if not isinstance(parsed, dict):
            return self._blank_state()
        state = self._blank_state()
        state.update(parsed)
        return state

    def _save_state(self, state: dict[str, object]) -> None:
        self._store.put(ENGINE_KEY, json.dumps(state, ensure_ascii=False))

    # -- mode control ------------------------------------------------------

    def arm_grab(self) -> dict[str, object]:
        """Operator storm override; idempotent within one armed episode."""

        with self._store.transaction():
            state = self._load_state()
            if not state["manual_grab"]:
                state["manual_grab"] = True
                state["grab_arm_seq"] = int(state["grab_arm_seq"]) + 1
                round_id = int(state["grab_arm_seq"])
                state["mode"] = Mode.GRAB.value
                state["exit_run"] = 0
                self._save_state(state)
                self._trail.append(
                    "GRAB_ARMED",
                    self._clock.now_epoch(),
                    f"GRAB_ARMED:manual:{round_id}",
                    {"manual": True, "arm_seq": round_id},
                )
            return self.mode_state()

    def release_grab(self) -> dict[str, object]:
        with self._store.transaction():
            state = self._load_state()
            changed = bool(state["manual_grab"])
            round_id = int(state["grab_arm_seq"])
            state["manual_grab"] = False
            if state["mode"] == Mode.GRAB.value:
                state["mode"] = Mode.AUTO.value
                state["grab_since_slot"] = None
                state["grab_max_turbidity"] = None
                state["exit_run"] = 0
            self._save_state(state)
            if changed:
                self._trail.append(
                    "GRAB_RELEASED",
                    self._clock.now_epoch(),
                    f"GRAB_RELEASED:manual:{round_id}",
                    {"manual": True, "arm_seq": round_id},
                )
            return self.mode_state()

    def mode_state(self) -> dict[str, object]:
        state = self._load_state()
        return {
            "mode": state["mode"],
            "manual_grab": state["manual_grab"],
            "exit_run": state["exit_run"],
            "grab_since_slot": state["grab_since_slot"],
            "last_slot": state["last_slot"],
        }

    def last_decision(self) -> dict[str, object] | None:
        decision = self._load_state()["last_decision"]
        return decision if isinstance(decision, dict) else None  # type: ignore[return-value]

    def totals(self) -> dict[str, object]:
        state = self._load_state()
        return {
            "cumulative_ml": state["cumulative_ml"],
            "totals_by_date": state["totals_by_date"],
        }

    # -- the tick ----------------------------------------------------------

    def tick(
        self,
        flow_m3h: float | None,
        turbidity_ntu: float | None,
        temperature_c: float | None,
        epoch: float | None = None,
    ) -> dict[str, object]:
        """Advance one aligned tick and return the stored decision snapshot."""

        now = self._clock.now_epoch() if epoch is None else epoch
        slot = slot_for(now, self._config.tick_seconds)

        with self._store.transaction():
            state = self._load_state()
            if state["last_slot"] == slot and state["last_decision"] is not None:
                # Same aligned tick replayed: return the stored decision
                # unchanged (idempotent), even though the request arrived late.
                decision = dict(state["last_decision"])
                decision["reasons"] = list(decision.get("reasons", [])) + [REASON_TICK_REPLAY]
                return decision
            if state["last_slot"] is not None and slot < float(state["last_slot"]):
                raise ValueError("cannot evaluate an alum tick in the past")

            # 1+2. Calendar first, then the as-of concentration read.
            self._calibration.maintain(slot)
            view = self._calibration.situation(slot)

            # 3a. Remember whatever the sensors still gave us, then resolve
            # each channel into OK / HOLD (recent last-known value) / SHUT
            # (gone too long to dose safely).
            reasons: list[str] = []
            last_known = dict(state["last_known"])
            for name, value in (
                ("flow", flow_m3h),
                ("turbidity", turbidity_ntu),
                ("temperature", temperature_c),
            ):
                if value is not None:
                    last_known[name] = {"value": float(value), "at": slot}
            state["last_known"] = last_known

            flow_state, flow_value, flow_reasons = self._channel_state(
                "flow", flow_m3h, last_known, slot,
                hold_reason=REASON_FLOW_HOLD,
                stale_reason=REASON_FLOW_SHUT,
            )
            turb_state, turb_value, turb_reasons = self._channel_state(
                "turbidity", turbidity_ntu, last_known, slot,
                hold_reason=REASON_TURB_HOLD,
                stale_reason=REASON_TURB_STALE_UPLIFT,
                missing_reason=REASON_TURB_UNKNOWN,
            )
            temp_entry = last_known.get("temperature")
            if temperature_c is not None:
                temp_value = float(temperature_c)
            elif temp_entry is not None:
                temp_value = float(temp_entry["value"])  # type: ignore[index]
                age = slot - float(temp_entry["at"])  # type: ignore[index]
                if age > 0:
                    reasons.append(REASON_TEMP_HOLD)
            else:
                temp_value = DEFAULT_TEMPERATURE_C
                reasons.append(REASON_TEMP_DEFAULT)
            reasons.extend(flow_reasons)
            reasons.extend(turb_reasons)

            window = [dict(item) for item in state["window"]]
            window.append(
                {
                    "slot": slot,
                    "flow": flow_value,
                    "turbidity": turb_value,
                    "flow_ok": flow_state == "OK",
                    "turbidity_ok": turb_state == "OK",
                }
            )
            window = window[-self._config.auto_window_ticks :]
            state["window"] = window

            # 3b. AUTO/GRAB latch, including the rate-of-rise trigger.
            rate_ntu_min = self._rate_of_rise(window, slot)
            mode, mode_reasons = self._resolve_mode(state, turb_value, rate_ntu_min, slot)
            reasons.extend(mode_reasons)

            # 4. Fixed policy for strength, basis and freeze/uplift actions.
            previous_spm = float(state["last_command_spm"])
            policy = _policy_for(view, mode, self._config, previous_spm > 0.0)
            reasons.extend(policy.reasons)

            flow_effective, turb_effective, basis_reasons = self._effective_inputs(
                mode, window, flow_value, turb_value, slot, state
            )
            reasons.extend(basis_reasons)

            # A turbidity channel stale long enough doses an uplift, never a
            # blind normal dose; a flow channel stale that long shuts the pump.
            turbidity_uplift = self._config.stale_uplift if turb_state == "STALE" else 1.0
            force_shutdown = flow_state == "STALE"
            flow_hold = flow_state == "HOLD"

            # 5. The dose chain, with every policy uplift applied to demand.
            inputs = DoseInputs(
                flow_m3h=max(0.0, flow_effective),
                turbidity_ntu=max(0.0, turb_effective),
                temperature_c=temp_value,
            )
            specific, turbidity_base, temperature_factor = specific_dose_mg_l(
                inputs, self._config
            )
            specific *= policy.uplift * turbidity_uplift
            mass = mass_rate_mg_min(specific, inputs.flow_m3h)
            target_solution = solution_flow_ml_min(
                mass, policy.concentration_pct, self._config.solution_density_g_ml
            )
            target_spm = self._config.pump.strokes_for_flow(target_solution)

            command_reasons: list[str] = []
            frozen = bool(policy.freeze_pump)
            if force_shutdown:
                commanded_spm = 0.0
            elif policy.freeze_pump or flow_hold:
                frozen = frozen or flow_hold
                commanded_spm = previous_spm
            else:
                commanded_spm = self._command_pump(target_spm, previous_spm, command_reasons)
            reasons.extend(command_reasons)

            # 6. Accounting integrates the *previous* command across the gap,
            # but never across a long hole (missed ticks / line downtime).
            last_slot = state["last_slot"]
            interval_seconds = 0.0 if last_slot is None else max(0.0, slot - float(last_slot))
            gap_integrated = (
                last_slot is not None and interval_seconds <= self._config.max_integrate_seconds
            )
            if last_slot is not None and not gap_integrated:
                reasons.append(REASON_GAP_NOT_INTEGRATED)
            delivered_ml = (
                previous_spm * self._config.pump.stroke_volume_ml * interval_seconds / 60.0
                if gap_integrated
                else 0.0
            )
            state["cumulative_ml"] = float(state["cumulative_ml"]) + delivered_ml
            date_key = to_iso(slot)[:10]
            totals_by_date = dict(state["totals_by_date"])
            totals_by_date[date_key] = float(totals_by_date.get(date_key, 0.0)) + delivered_ml
            state["totals_by_date"] = totals_by_date

            previous_concentration = (
                None
                if view.previous_version is None
                else view.previous_version.concentration_pct
            )
            decision: dict[str, object] = {
                "tick_id": f"{int(slot // self._config.tick_seconds)}",
                "slot_epoch": slot,
                "at": to_iso(slot),
                "mode": mode.value,
                "dose_basis": policy.basis.value,
                "situation": view.situation.value,
                "reasons": reasons,
                "concentration": {
                    "used_pct": policy.concentration_pct,
                    "snapshot_pct": view.concentration_pct,
                    "active_version_id": None if view.version is None else view.version.id,
                    "previous_version_id": (
                        None if view.previous_version is None else view.previous_version.id
                    ),
                    "previous_pct": previous_concentration,
                    "age_seconds": view.age_seconds,
                    "active_ticket_id": None if view.active_ticket is None else view.active_ticket.id,
                    "ticket_status": None if view.active_ticket is None else view.active_ticket.status,
                    "uplift": policy.uplift,
                },
                "inputs": {
                    "raw_flow_m3h": flow_m3h,
                    "raw_turbidity_ntu": turbidity_ntu,
                    "raw_temperature_c": temperature_c,
                    "flow_m3h": inputs.flow_m3h,
                    "turbidity_ntu": inputs.turbidity_ntu,
                    "temperature_c": inputs.temperature_c,
                    "window_size": len(window),
                    "window_mean_flow_m3h": self._mean(window, "flow"),
                    "window_mean_turbidity_ntu": self._mean(window, "turbidity"),
                    "turbidity_rate_ntu_per_min": rate_ntu_min,
                },
                "dose": {
                    "turbidity_base_mg_l": turbidity_base,
                    "temperature_factor": temperature_factor,
                    "specific_dose_mg_l": specific,
                    "mass_mg_min": mass,
                    "solution_ml_min": target_solution,
                },
                "pump": {
                    "target_spm": target_spm,
                    "command_spm": commanded_spm,
                    "frozen": frozen,
                    "safe_shutdown": force_shutdown,
                    "stroke_volume_ml": self._config.pump.stroke_volume_ml,
                },
                "accounting": {
                    "interval_seconds": interval_seconds,
                    "gap_integrated": gap_integrated,
                    "delivered_ml": delivered_ml,
                    "cumulative_ml": float(state["cumulative_ml"]),
                    "day_total_ml": float(totals_by_date[date_key]),
                },
            }

            row = self._trail.append(
                kind="ALUM_DECISION",
                at_epoch=slot,
                idempotency_key=f"ALUM_DECISION:{decision['tick_id']}",
                payload=decision,
            )
            decision["seq"] = row.seq

            state["last_slot"] = slot
            state["last_command_spm"] = commanded_spm
            state["last_seq"] = row.seq
            state["last_decision"] = decision
            self._save_state(state)
            return dict(decision)

    # -- helpers -----------------------------------------------------------

    def _channel_state(
        self,
        name: str,
        raw: float | None,
        last_known: dict[str, object],
        slot: float,
        hold_reason: str,
        stale_reason: str,
        missing_reason: str | None = None,
    ) -> tuple[str, float, list[str]]:
        """Classify one sensor channel as OK / HOLD / STALE / MISSING."""

        if raw is not None:
            return "OK", float(raw), []
        entry = last_known.get(name)
        if entry is None:
            return "MISSING", 0.0, ([missing_reason] if missing_reason else [])
        value = float(entry["value"])  # type: ignore[index]
        age = slot - float(entry["at"])  # type: ignore[index]
        if age <= self._config.sensor_hold_seconds:
            return "HOLD", value, [hold_reason]
        return "STALE", value, [stale_reason]

    def _rate_of_rise(self, window: list[dict[str, object]], slot: float) -> float:
        valid = [item for item in window if item.get("turbidity_ok")]
        if len(valid) < 2:
            return 0.0
        span_minutes = max(
            1e-9,
            (float(valid[-1]["slot"]) - float(valid[0]["slot"])) / 60.0,
        )
        return (float(valid[-1]["turbidity"]) - float(valid[0]["turbidity"])) / span_minutes

    def _resolve_mode(
        self,
        state: dict[str, object],
        turbidity: float,
        rate_ntu_min: float,
        slot: float,
    ) -> tuple[Mode, list[str]]:
        reasons: list[str] = []
        if state["manual_grab"]:
            state["mode"] = Mode.GRAB.value
            if state["grab_since_slot"] is None:
                state["grab_since_slot"] = slot
            return Mode.GRAB, [REASON_GRAB_MANUAL]

        entered = False
        if turbidity >= self._config.grab_enter_ntu:
            reasons.append(REASON_GRAB_AUTO_ENTER)
            entered = True
        elif rate_ntu_min >= self._config.grab_rate_ntu_per_min:
            reasons.append(REASON_GRAB_RATE_ENTER)
            entered = True

        if state["mode"] == Mode.GRAB.value:
            if turbidity <= self._config.grab_exit_ntu:
                state["exit_run"] = int(state["exit_run"]) + 1
            else:
                state["exit_run"] = 0
            if state["exit_run"] >= self._config.grab_exit_ticks and not entered:
                state["mode"] = Mode.AUTO.value
                state["exit_run"] = 0
                state["grab_since_slot"] = None
                state["grab_max_turbidity"] = None
                reasons.append(REASON_GRAB_EXIT)
                return Mode.AUTO, reasons
            return Mode.GRAB, reasons

        if entered:
            state["mode"] = Mode.GRAB.value
            state["exit_run"] = 0
            state["grab_since_slot"] = slot
            state["grab_max_turbidity"] = None
            return Mode.GRAB, reasons

        return Mode.AUTO, reasons

    def _effective_inputs(
        self,
        mode: Mode,
        window: list[dict[str, object]],
        flow_value: float,
        turb_value: float,
        slot: float,
        state: dict[str, object],
    ) -> tuple[float, float, list[str]]:
        if mode == Mode.AUTO:
            return self._mean(window, "flow"), self._mean(window, "turbidity"), []
        # GRAB: instantaneous flow, and turbidity max-held for the whole storm
        # episode (persisted, so it survives a window truncation/restart).
        held = state.get("grab_max_turbidity")
        held_value = float(held) if held is not None else turb_value
        held_value = max(held_value, turb_value)
        state["grab_max_turbidity"] = held_value
        return flow_value, held_value, []

    @staticmethod
    def _mean(window: list[dict[str, object]], key: str) -> float:
        values = [float(item[key]) for item in window if item.get(key) is not None]
        return sum(values) / len(values) if values else 0.0

    def _command_pump(
        self, target_spm: float, previous_spm: float, reasons: list[str]
    ) -> float:
        pump = self._config.pump
        if target_spm >= pump.max_strokes_per_min:
            reasons.append(REASON_PUMP_CLAMPED_MAX)
        candidate, rate_limited = pump.apply_limits(target_spm, previous_spm)
        delta = abs(target_spm - previous_spm)
        if delta <= pump.deadband_fraction * max(previous_spm, 1e-9):
            if target_spm not in (0.0, previous_spm):
                reasons.append(REASON_PUMP_DEADBAND)
        if rate_limited:
            reasons.append(REASON_PUMP_RATE_LIMITED)
        if target_spm > 0 and candidate == 0.0 and previous_spm == 0.0:
            reasons.append(REASON_PUMP_CUTOFF)
        return candidate

    # -- console projection ------------------------------------------------

    def describe(self) -> str:
        decision = self.last_decision()
        state = self._load_state()
        if decision is None:
            return "alum dosing idle no decision yet"
        return (
            f"alum mode={decision['mode']} basis={decision['dose_basis']} "
            f"situation={decision['situation']} "
            f"concentration={decision['concentration']['used_pct']:.3f}% "
            f"command_spm={decision['pump']['command_spm']:.3f} "
            f"cumulative_ml={float(state['cumulative_ml']):.1f}"
        )
