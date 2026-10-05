"""Tests for the shift-calibrated alum dosing subsystem."""

from __future__ import annotations

import tempfile
import unittest

from waterplant.coag import (
    AlumConfig,
    AlumEngine,
    AuditTrail,
    CalibrationService,
    Curve,
    DoseBasis,
    DoseInputs,
    FakeClock,
    Mode,
    Point,
    Pump,
    Situation,
    TicketStatus,
    load_config,
    mass_rate_mg_min,
    save_config,
    shift_start_for,
    slot_for,
    solution_flow_ml_min,
    specific_dose_mg_l,
)
from waterplant.coag.engine import (
    REASON_AUTO_MEAN,
    REASON_FLOW_HOLD,
    REASON_FLOW_SHUT,
    REASON_GRAB_AUTO_ENTER,
    REASON_GRAB_INSTANT,
    REASON_GRAB_MANUAL,
    REASON_NONE_FLOOR,
    REASON_PENDING_AUTO_OLD,
    REASON_PENDING_GRAB_OLD,
    REASON_PENDING_GRAB_WAIT,
    REASON_STALE_FLOOR,
    REASON_TICK_REPLAY,
)
from waterplant.store import Store


def build_engine(tmp: str, **overrides) -> tuple[AlumEngine, FakeClock]:
    store = Store.open(f"{tmp}/state.json")
    config = AlumConfig()
    config.staleness_seconds = 12 * 3600
    config.shift_hours = 8
    for name, value in overrides.items():
        setattr(config, name, value)
    save_config(store, config)
    clock = FakeClock(0.0)
    engine = AlumEngine(store, config, clock)
    return engine, clock


def accept_calibration(
    engine: AlumEngine, epoch: float, concentration: float = 10.0
) -> str:
    service = engine.calibration
    ticket, _ = service.open_ticket(epoch=epoch)
    for replicate in range(1, engine.config.required_readings + 1):
        ticket, _ = service.submit_reading(
            ticket.id, replicate, concentration, epoch=epoch + replicate
        )
    assert ticket.status == TicketStatus.ACCEPTED.value
    return ticket.version_id


class ModelCase(unittest.TestCase):
    def test_curve_interpolates_and_clamps(self) -> None:
        curve = Curve([Point(0.0, 1.0), Point(10.0, 3.0), Point(20.0, 9.0)])
        self.assertEqual(curve.lookup(-5), 1.0)
        self.assertEqual(curve.lookup(0), 1.0)
        self.assertEqual(curve.lookup(5), 2.0)
        self.assertEqual(curve.lookup(10), 3.0)
        self.assertEqual(curve.lookup(25), 9.0)

    def test_curve_rejects_bad_points(self) -> None:
        with self.assertRaises(ValueError):
            Curve([Point(0.0, 1.0)])
        with self.assertRaises(ValueError):
            Curve([Point(1.0, 1.0), Point(1.0, 2.0)])

    def test_mass_and_solution_units(self) -> None:
        # 12 mg/L at 60 m3/h = 12,000 mg/min.
        self.assertAlmostEqual(mass_rate_mg_min(12.0, 60.0), 12_000.0)
        # 12,000 mg/min at 10% strength and 1.0 g/mL: 1 mL carries 100 mg, so
        # the pump feeds 120 mL/min.
        self.assertAlmostEqual(
            solution_flow_ml_min(12_000.0, 10.0, 1.0), 120.0, places=6
        )
        with self.assertRaises(ValueError):
            solution_flow_ml_min(1.0, 0.0, 1.0)

    def test_dose_blends_turbidity_temperature_flow_strength(self) -> None:
        config = AlumConfig()
        warm = specific_dose_mg_l(DoseInputs(1000.0, 100.0, 20.0), config)[0]
        cold = specific_dose_mg_l(DoseInputs(1000.0, 100.0, 5.0), config)[0]
        self.assertGreater(cold, warm)
        self.assertAlmostEqual(cold / warm, 1.18)
        strong = solution_flow_ml_min(
            mass_rate_mg_min(warm, 1000.0), 10.0, config.solution_density_g_ml
        )
        weak = solution_flow_ml_min(
            mass_rate_mg_min(warm, 1000.0), 5.0, config.solution_density_g_ml
        )
        # Halving the prepared strength doubles the solution the pump feeds.
        self.assertAlmostEqual(weak / strong, 2.0)

    def test_pump_stroke_limits(self) -> None:
        pump = Pump(stroke_volume_ml=100.0, max_strokes_per_min=10.0, max_step_spm=2.0)
        self.assertEqual(pump.strokes_for_flow(5000.0), 10.0)  # clamped
        self.assertEqual(pump.strokes_for_flow(1.0), 0.0)  # below cutoff
        command, limited = pump.apply_limits(10.0, 0.0)
        self.assertEqual(command, 2.0)
        self.assertTrue(limited)
        command, limited = pump.apply_limits(2.001, 2.0)
        self.assertEqual(command, 2.0)  # deadband
        self.assertFalse(limited)

    def test_config_round_trip_and_validation(self) -> None:
        config = AlumConfig()
        restored = AlumConfig.from_json(config.to_json())
        self.assertEqual(
            [(p.x, p.y) for p in restored.turbidity_curve],
            [(p.x, p.y) for p in config.turbidity_curve],
        )
        config.shift_hours = 0
        with self.assertRaises(ValueError):
            config.validate()


class ShiftCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.config = AlumConfig()
        self.config.shift_hours = 8
        self.clock = FakeClock(3600.0)
        self.service = CalibrationService(store, self.config, self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shift_boundary_alignment(self) -> None:
        self.assertEqual(shift_start_for(3600.0, 8), 0.0)
        self.assertEqual(shift_start_for(9 * 3600.0, 8), 8 * 3600.0)
        self.assertEqual(slot_for(90.0, 60.0), 60.0)

    def test_open_ticket_is_idempotent_within_shift(self) -> None:
        first, first_events = self.service.open_ticket(epoch=100.0)
        second, second_events = self.service.open_ticket(epoch=200.0)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(first_events), 1)
        self.assertEqual(second_events, [])

    def test_full_acceptance_publishes_versioned_strength(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        ticket, _ = self.service.submit_reading(ticket.id, 1, 10.0, epoch=10.0)
        self.assertIsNone(self.service.latest_version())
        ticket, _ = self.service.submit_reading(ticket.id, 2, 10.4, epoch=20.0)
        ticket, _ = self.service.submit_reading(ticket.id, 3, 9.6, epoch=30.0)
        self.assertEqual(ticket.status, TicketStatus.ACCEPTED.value)
        self.assertAlmostEqual(ticket.concentration_pct, 10.0)
        version = self.service.latest_version()
        self.assertIsNotNone(version)
        self.assertAlmostEqual(version.concentration_pct, 10.0)
        # As-of reads: nothing was in force before the acceptance instant.
        self.assertIsNone(self.service.version_as_of(25.0))
        self.assertEqual(self.service.version_as_of(30.0).id, version.id)

    def test_duplicate_reading_submit_is_idempotent(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        ticket, events_a = self.service.submit_reading(ticket.id, 1, 10.0, epoch=10.0)
        ticket, events_b = self.service.submit_reading(ticket.id, 1, 10.0, epoch=11.0)
        self.assertEqual(len(events_a), 1)
        self.assertEqual(events_b, [])
        self.assertEqual(len(ticket.readings), 1)

    def test_out_of_band_calibration_is_rejected_without_strength_change(self) -> None:
        accept = self._accept(0.0, 10.0)
        bad, _ = self.service.open_ticket(epoch=40.0)
        for replicate in range(1, 4):
            bad, _ = self.service.submit_reading(bad.id, replicate, 99.0, epoch=40 + replicate)
        self.assertEqual(bad.status, TicketStatus.REJECTED.value)
        self.assertEqual(self.service.latest_version().id, accept)
        self.assertAlmostEqual(self.service.latest_version().concentration_pct, 10.0)

    def test_readings_on_terminal_ticket_are_noop(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        for replicate in range(1, 4):
            self.service.submit_reading(ticket.id, replicate, 10.0, epoch=float(replicate))
        accepted = self.service.ticket(ticket.id)
        again, events = self.service.submit_reading(accepted.id, 2, 5.0, epoch=99.0)
        self.assertEqual(again.status, TicketStatus.ACCEPTED.value)
        self.assertEqual(events, [])
        lost, events = self.service.report_lost_reading(accepted.id, 2, epoch=100.0)
        self.assertEqual(lost.status, TicketStatus.ACCEPTED.value)
        self.assertEqual(events, [])

    def test_lost_reading_is_partial_failure_with_idempotent_recovery(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        self.service.submit_reading(ticket.id, 1, 10.0, epoch=10.0)
        ticket, _ = self.service.report_lost_reading(ticket.id, 2, epoch=20.0)
        self.assertEqual(ticket.status, TicketStatus.READING_LOST.value)
        self.assertEqual(ticket.lost_replicates, [2])
        # Reporting the same loss again changes nothing.
        again, events = self.service.report_lost_reading(ticket.id, 2, epoch=21.0)
        self.assertEqual(again.lost_replicates, [2])
        self.assertEqual(events, [])
        recovered, events = self.service.recover(ticket.id, epoch=30.0)
        self.assertEqual(recovered.status, TicketStatus.OPEN.value)
        self.assertEqual(recovered.recovery_count, 1)
        self.assertEqual(len(events), 1)
        # Recovery is idempotent: a repeated trigger produces no new event.
        again, events = self.service.recover(ticket.id, epoch=31.0)
        self.assertEqual(again.recovery_count, 1)
        self.assertEqual(events, [])
        self.service.submit_reading(ticket.id, 2, 9.8, epoch=40.0)
        ticket, _ = self.service.submit_reading(ticket.id, 3, 10.2, epoch=50.0)
        self.assertEqual(ticket.status, TicketStatus.ACCEPTED.value)
        # Recovering a finished ticket is a no-op.
        finished, events = self.service.recover(ticket.id, epoch=60.0)
        self.assertEqual(finished.status, TicketStatus.ACCEPTED.value)
        self.assertEqual(events, [])

    def test_overdue_ticket_escalates_through_maintain(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        events = self.service.maintain(epoch=ticket.due_epoch + 1)
        self.assertEqual(ticket.id, self.service.active_ticket().id)
        self.assertEqual(self.service.active_ticket().status, TicketStatus.READING_LOST.value)
        self.assertTrue(any(e["kind"] == "CALIB_OVERDUE" for e in events))

    def _accept(self, epoch: float, concentration: float) -> str:
        ticket, _ = self.service.open_ticket(epoch=epoch)
        for replicate in range(1, 4):
            ticket, _ = self.service.submit_reading(
                ticket.id, replicate, concentration, epoch=epoch + replicate
            )
        return ticket.version_id


class SituationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.config = AlumConfig()
        self.config.shift_hours = 8
        self.config.staleness_seconds = 24 * 3600
        self.clock = FakeClock(0.0)
        self.service = CalibrationService(store, self.config, self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_none_before_first_acceptance_uses_floor(self) -> None:
        view = self.service.situation(epoch=100.0)
        self.assertEqual(view.situation, Situation.NONE)
        self.assertEqual(view.concentration_pct, self.config.floor_concentration_pct)
        self.assertIn("NO_ACCEPTED_VERSION", view.reasons)

    def test_pending_keeps_old_strength_until_acceptance(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        for replicate in range(1, 4):
            self.service.submit_reading(ticket.id, replicate, 10.0, epoch=replicate)
        # Fresh version accepted at t=3; a later open ticket must not change it.
        self.service.maintain(epoch=8 * 3600 + 1)
        pending = self.service.active_ticket()
        self.assertIsNotNone(pending)
        view = self.service.situation(epoch=8 * 3600 + 100)
        self.assertEqual(view.situation, Situation.PENDING)
        self.assertEqual(view.concentration_pct, 10.0)
        self.assertIsNotNone(view.version)
        self.assertTrue(any("PENDING" in reason for reason in view.reasons))

    def test_stale_after_staleness_window_uses_floor(self) -> None:
        ticket, _ = self.service.open_ticket(epoch=0.0)
        for replicate in range(1, 4):
            self.service.submit_reading(ticket.id, replicate, 9.0, epoch=replicate)
        view = self.service.situation(epoch=3 + self.config.staleness_seconds + 1)
        self.assertEqual(view.situation, Situation.STALE)
        self.assertEqual(view.concentration_pct, self.config.floor_concentration_pct)
        self.assertIn("VERSION_PAST_STALENESS", view.reasons)


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine, self.clock = build_engine(
            self._tmp.name, grab_pending_use_old=True
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tick(self, flow, turbidity, temperature, epoch):
        return self.engine.tick(flow, turbidity, temperature, epoch=epoch)

    def test_auto_uses_window_mean_and_fresh_concentration(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        first = self.tick(600.0, 20.0, 20.0, 60.0)
        self.assertEqual(first["mode"], Mode.AUTO.value)
        self.assertEqual(first["dose_basis"], DoseBasis.MEAN.value)
        self.assertAlmostEqual(first["inputs"]["flow_m3h"], 600.0)
        self.assertAlmostEqual(first["concentration"]["used_pct"], 10.0)
        self.assertTrue(any(r == REASON_AUTO_MEAN for r in first["reasons"]))

    def test_storm_grab_switches_to_instant_and_max_hold(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 30.0, 10.0, 60.0)
        storm = self.tick(1000.0, 500.0, 10.0, 120.0)
        self.assertEqual(storm["mode"], Mode.GRAB.value)
        self.assertEqual(storm["dose_basis"], DoseBasis.INSTANT.value)
        self.assertAlmostEqual(storm["inputs"]["turbidity_ntu"], 500.0)
        self.assertTrue(any(r == REASON_GRAB_AUTO_ENTER for r in storm["reasons"]))
        self.assertTrue(any(r == REASON_GRAB_INSTANT for r in storm["reasons"]))
        # Spiky drop still max-holds the storm peak.
        dip = self.tick(1000.0, 50.0, 10.0, 180.0)
        self.assertAlmostEqual(dip["inputs"]["turbidity_ntu"], 500.0)

    def test_rate_of_rise_arms_grab(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.engine.config.grab_enter_ntu = 1000.0  # only RoR can trigger now
        self.tick(1000.0, 10.0, 10.0, 60.0)
        spike = self.tick(1000.0, 500.0, 10.0, 120.0)  # 490 NTU/min
        self.assertEqual(spike["mode"], Mode.GRAB.value)

    def test_pending_auto_uses_old_strength_until_confirmation(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        # Open the next shift ticket but do not complete it.
        self.engine.calibration.open_ticket(epoch=8 * 3600)
        decision = self.tick(1000.0, 30.0, 15.0, 8 * 3600 + 60)
        self.assertEqual(decision["situation"], Situation.PENDING.value)
        self.assertAlmostEqual(decision["concentration"]["used_pct"], 10.0)
        self.assertTrue(any(r == REASON_PENDING_AUTO_OLD for r in decision["reasons"]))

    def test_grab_pending_policy_covers_spike_with_old_strength(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.engine.calibration.open_ticket(epoch=8 * 3600)
        decision = self.tick(1000.0, 500.0, 10.0, 8 * 3600 + 60)
        self.assertEqual(decision["mode"], Mode.GRAB.value)
        self.assertAlmostEqual(decision["concentration"]["used_pct"], 10.0)
        self.assertTrue(any(r == REASON_PENDING_GRAB_OLD for r in decision["reasons"]))
        self.assertFalse(decision["pump"]["frozen"])

    def test_grab_pending_wait_policy_freezes_until_confirmation(self) -> None:
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine, _ = build_engine(self._tmp.name, grab_pending_use_old=False)
        accept_calibration(self.engine, 0.0, 10.0)
        # Establish a running command first.
        self.tick(1000.0, 30.0, 10.0, 60.0)
        self.engine.calibration.open_ticket(epoch=8 * 3600)
        frozen = self.tick(1000.0, 500.0, 10.0, 8 * 3600 + 60)
        self.assertTrue(frozen["pump"]["frozen"])
        self.assertTrue(any(r == REASON_PENDING_GRAB_WAIT for r in frozen["reasons"]))

    def test_floor_strength_when_no_version_and_when_stale(self) -> None:
        # No calibration at all: bootstrap floor + uplift.
        missing = self.tick(1000.0, 30.0, 10.0, 60.0)
        self.assertEqual(missing["situation"], Situation.NONE.value)
        self.assertAlmostEqual(
            missing["concentration"]["used_pct"], self.engine.config.floor_concentration_pct
        )
        self.assertAlmostEqual(missing["concentration"]["uplift"], self.engine.config.stale_uplift)
        self.assertTrue(any(r == REASON_NONE_FLOOR for r in missing["reasons"]))

        accept_calibration(self.engine, 100.0, 9.0)
        stale = self.tick(1000.0, 30.0, 10.0, 100.0 + 13 * 3600)
        self.assertEqual(stale["situation"], Situation.STALE.value)
        self.assertAlmostEqual(
            stale["concentration"]["used_pct"], self.engine.config.floor_concentration_pct
        )
        self.assertTrue(any(r == REASON_STALE_FLOOR for r in stale["reasons"]))

    def test_tick_replay_is_idempotent(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        decision = self.tick(1000.0, 30.0, 10.0, 60.0)
        rows_before = len(self.engine.trail.rows())
        replay = self.tick(999.0, 999.0, 9.0, 60.0)
        self.assertEqual(replay["seq"], decision["seq"])
        self.assertEqual(
            replay["pump"]["command_spm"], decision["pump"]["command_spm"]
        )
        self.assertTrue(any(r == REASON_TICK_REPLAY for r in replay["reasons"]))
        self.assertEqual(len(self.engine.trail.rows()), rows_before)

    def test_sensor_loss_hold_then_safe_shutdown(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        running = self.tick(1000.0, 500.0, 10.0, 60.0)
        held = self.tick(None, 500.0, 10.0, 120.0)
        self.assertTrue(held["pump"]["frozen"])
        self.assertAlmostEqual(
            held["pump"]["command_spm"], running["pump"]["command_spm"]
        )
        self.assertTrue(any(r == REASON_FLOW_HOLD for r in held["reasons"]))
        shutdown = self.tick(None, 500.0, 10.0, 120.0 + 16 * 60)
        self.assertTrue(shutdown["pump"]["safe_shutdown"])
        self.assertEqual(shutdown["pump"]["command_spm"], 0.0)
        self.assertTrue(any(r == REASON_FLOW_SHUT for r in shutdown["reasons"]))

    def test_arm_release_arm_cycles_are_separate_audit_episodes(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 10.0, 10.0, 60.0)
        self.engine.arm_grab()
        self.engine.release_grab()
        self.engine.arm_grab()
        self.engine.release_grab()
        kinds = [(r.kind, r.idempotency_key) for r in self.engine.trail.rows()
                 if r.kind in ("GRAB_ARMED", "GRAB_RELEASED")]
        self.assertEqual(len(kinds), 4)
        self.assertEqual(len({key for _, key in kinds}), 4)

    def test_temperature_hold_records_reason(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 30.0, 5.0, 60.0)
        held = self.tick(1000.0, 30.0, None, 120.0)
        self.assertAlmostEqual(held["inputs"]["temperature_c"], 5.0)
        self.assertIn("TEMPERATURE_SENSOR_LOST_USING_LAST_KNOWN", held["reasons"])

    def test_manual_grab_and_release(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 10.0, 10.0, 60.0)
        state = self.engine.arm_grab()
        self.assertEqual(state["mode"], Mode.GRAB.value)
        # Arming twice is idempotent.
        self.engine.arm_grab()
        armed = self.tick(1000.0, 10.0, 10.0, 120.0)
        self.assertTrue(any(r == REASON_GRAB_MANUAL for r in armed["reasons"]))
        self.assertEqual(armed["dose_basis"], DoseBasis.INSTANT.value)
        released = self.engine.release_grab()
        self.assertEqual(released["mode"], Mode.AUTO.value)

    def test_long_tick_gap_is_not_integrated(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 30.0, 10.0, 60.0)
        gapped = self.tick(1000.0, 30.0, 10.0, 60.0 + 10 * 60)
        self.assertFalse(gapped["accounting"]["gap_integrated"])
        self.assertEqual(gapped["accounting"]["delivered_ml"], 0.0)
        self.assertIn("TICK_GAP_TOO_LONG_VOLUME_NOT_INTEGRATED", gapped["reasons"])

    def test_accounting_accumulates_previous_command_over_interval(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        first = self.tick(1000.0, 30.0, 10.0, 60.0)
        self.assertEqual(first["accounting"]["interval_seconds"], 0.0)
        second = self.tick(1000.0, 30.0, 10.0, 120.0)
        expected = (
            first["pump"]["command_spm"]
            * self.engine.config.pump.stroke_volume_ml
            * 60.0
            / 60.0
        )
        self.assertAlmostEqual(second["accounting"]["delivered_ml"], expected)
        self.assertAlmostEqual(
            second["accounting"]["cumulative_ml"], expected
        )
        self.assertGreater(second["accounting"]["day_total_ml"], 0.0)

    def test_refresh_lands_only_on_following_tick(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        before = self.tick(1000.0, 30.0, 10.0, 8 * 3600)
        self.assertAlmostEqual(before["concentration"]["used_pct"], 10.0)
        # Accept the new calibration *after* that tick's as-of read.
        accept_calibration(self.engine, 8 * 3600 + 30, 8.0)
        after = self.tick(1000.0, 30.0, 10.0, 8 * 3600 + 60)
        self.assertAlmostEqual(after["concentration"]["used_pct"], 8.0)
        self.assertAlmostEqual(after["concentration"]["previous_pct"], 10.0)
        self.assertEqual(after["situation"], Situation.FRESH.value)

    def test_decision_payload_is_self_consistent(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        decision = self.tick(1000.0, 100.0, 5.0, 60.0)
        concentration = decision["concentration"]["used_pct"]
        inputs = decision["inputs"]
        dose = decision["dose"]
        recomputed_mass = (
            dose["specific_dose_mg_l"]
            * inputs["flow_m3h"]
            * 1000.0
            / 60.0
        )
        self.assertAlmostEqual(recomputed_mass, dose["mass_mg_min"], places=3)
        recomputed_solution = dose["mass_mg_min"] / (
            self.engine.config.solution_density_g_ml * concentration * 10.0
        )
        self.assertAlmostEqual(
            recomputed_solution, dose["solution_ml_min"], places=3
        )
        self.assertEqual(
            decision["pump"]["command_spm"],
            min(
                self.engine.config.pump.max_strokes_per_min,
                max(0.0, dose["solution_ml_min"] / self.engine.config.pump.stroke_volume_ml),
            ),
        )

    def test_engine_state_persists_across_restart(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        decision = self.tick(1000.0, 500.0, 10.0, 60.0)
        store = self.engine._store  # noqa: SLF001 - restart shares the file
        restarted = AlumEngine(store, self.engine.config, FakeClock(120.0))
        self.assertEqual(restarted.mode_state()["mode"], Mode.GRAB.value)
        self.assertEqual(restarted.last_decision()["seq"], decision["seq"])

    def test_past_tick_outside_replay_is_rejected(self) -> None:
        accept_calibration(self.engine, 0.0, 10.0)
        self.tick(1000.0, 30.0, 10.0, 120.0)
        with self.assertRaises(ValueError):
            self.tick(1000.0, 30.0, 10.0, 60.0)


class TrailCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.trail = AuditTrail(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rows_chain_and_idempotent_keys_dedup(self) -> None:
        first = self.trail.append("ALUM_DECISION", 1.0, "k1", {"a": 1})
        second = self.trail.append("ALUM_DECISION", 2.0, "k2", {"a": 2})
        self.assertEqual(first.seq, 1)
        self.assertEqual(second.seq, 2)
        self.assertEqual(second.prev_hash, first.row_hash)
        replay = self.trail.append("ALUM_DECISION", 9.0, "k1", {"a": 1})
        self.assertEqual(replay.seq, first.seq)
        self.assertEqual(len(self.trail.rows()), 2)
        self.assertTrue(self.trail.verify().ok)

    def test_chain_detects_tampering(self) -> None:
        self.trail.append("ALUM_DECISION", 1.0, "k1", {"a": 1})
        self.trail.append("ALUM_DECISION", 2.0, "k2", {"a": 2})
        raw = self.store.get("coag:alum:audit")[0]
        tampered = raw.replace('"a": 2', '"a": 5')
        self.store.put("coag:alum:audit", tampered)
        verification = AuditTrail(self.store).verify()
        self.assertFalse(verification.ok)
        self.assertEqual(verification.broken_at_seq, 2)


if __name__ == "__main__":
    unittest.main()
