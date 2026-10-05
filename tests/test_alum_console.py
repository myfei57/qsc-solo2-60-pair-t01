"""End-to-end console tests for the alum dosing endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.routes import route_table
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store

ALUM_ROUTES = {
    ("POST", "/alum/tick"),
    ("GET", "/alum/last"),
    ("GET", "/alum/panel"),
    ("GET", "/alum/state"),
    ("GET", "/alum/mode"),
    ("POST", "/alum/grab/arm"),
    ("POST", "/alum/grab/release"),
    ("GET", "/alum/concentration"),
    ("GET", "/alum/versions"),
    ("POST", "/alum/tickets/open"),
    ("GET", "/alum/tickets"),
    ("GET", "/alum/ticket"),
    ("POST", "/alum/reading"),
    ("POST", "/alum/reading/lost"),
    ("POST", "/alum/recover"),
    ("POST", "/alum/reject"),
    ("GET", "/alum/audit"),
    ("GET", "/alum/audit/verify"),
    ("GET", "/alum/config"),
    ("PUT", "/alum/config"),
}


class AlumConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)
        self.epoch = 3600

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def calibrate(self, concentration: float = 10.0, epoch: int | None = None) -> str:
        epoch = self.epoch if epoch is None else epoch
        status, opened = self.call("POST", "/alum/tickets/open", {"epoch": epoch})
        self.assertEqual(status, 200)
        ticket_id = opened["ticket"]["id"]
        for replicate in range(1, 4):
            status, payload = self.call(
                "POST",
                "/alum/reading",
                {
                    "ticket_id": ticket_id,
                    "replicate": replicate,
                    "value_pct": concentration,
                    "epoch": epoch + replicate,
                },
            )
            self.assertEqual(status, 200)
        self.assertEqual(payload["ticket"]["status"], "ACCEPTED")
        return ticket_id

    def test_all_alum_routes_are_catalogued(self) -> None:
        catalogued = {(route.method, route.path) for route in route_table()}
        self.assertTrue(ALUM_ROUTES.issubset(catalogued))

    def test_calibration_then_tick_uses_refreshed_strength(self) -> None:
        self.calibrate(10.0)
        status, decision = self.call(
            "POST",
            "/alum/tick",
            {"flow": 1000, "turbidity": 30, "temperature": 5, "epoch": self.epoch + 60},
        )
        self.assertEqual(status, 200)
        self.assertEqual(decision["situation"], "FRESH")
        self.assertEqual(decision["mode"], "AUTO")
        self.assertEqual(decision["dose_basis"], "MEAN")
        self.assertAlmostEqual(decision["concentration"]["used_pct"], 10.0)

    def test_floor_strength_before_first_calibration(self) -> None:
        status, decision = self.call(
            "POST",
            "/alum/tick",
            {"flow": 1000, "turbidity": 30, "temperature": 5, "epoch": self.epoch},
        )
        self.assertEqual(status, 200)
        self.assertEqual(decision["situation"], "NONE")
        self.assertEqual(decision["concentration"]["used_pct"], 8.0)

    def test_lost_reading_recovery_is_idempotent_over_http(self) -> None:
        status, opened = self.call("POST", "/alum/tickets/open", {"epoch": self.epoch})
        ticket_id = opened["ticket"]["id"]
        self.call(
            "POST",
            "/alum/reading",
            {"ticket_id": ticket_id, "replicate": 1, "value_pct": 10.0,
             "epoch": self.epoch + 10},
        )
        status, lost = self.call(
            "POST",
            "/alum/reading/lost",
            {"ticket_id": ticket_id, "replicate": 2, "epoch": self.epoch + 20},
        )
        self.assertEqual(lost["ticket"]["status"], "READING_LOST")
        status, first = self.call(
            "POST", "/alum/recover", {"ticket_id": ticket_id, "epoch": self.epoch + 30}
        )
        status, second = self.call(
            "POST", "/alum/recover", {"ticket_id": ticket_id, "epoch": self.epoch + 40}
        )
        self.assertEqual(first["ticket"]["recovery_count"], 1)
        self.assertEqual(second["ticket"]["recovery_count"], 1)
        self.assertEqual(second["events"], [])

    def test_same_tick_replayed_returns_stored_decision(self) -> None:
        self.calibrate(10.0)
        payload = {"flow": 1000, "turbidity": 30, "temperature": 5, "epoch": self.epoch + 60}
        _, first = self.call("POST", "/alum/tick", payload)
        _, replay = self.call(
            "POST",
            "/alum/tick",
            {"flow": 1, "turbidity": 1, "temperature": 1, "epoch": self.epoch + 60},
        )
        self.assertEqual(replay["seq"], first["seq"])
        self.assertEqual(
            replay["pump"]["command_spm"], first["pump"]["command_spm"]
        )
        self.assertIn("TICK_SLOT_REPLAY_RETURNED_STORED_DECISION", replay["reasons"])

    def test_storm_grab_and_manual_arm(self) -> None:
        self.calibrate(10.0)
        _, storm = self.call(
            "POST",
            "/alum/tick",
            {"flow": 1000, "turbidity": 500, "temperature": 10, "epoch": self.epoch + 60},
        )
        self.assertEqual(storm["mode"], "GRAB")
        self.assertEqual(storm["dose_basis"], "INSTANT")
        status, mode = self.call("GET", "/alum/mode")
        self.assertEqual(mode["mode"], "GRAB")
        status, released = self.call("POST", "/alum/grab/release")
        self.assertEqual(released["mode"], "AUTO")
        status, armed = self.call("POST", "/alum/grab/arm")
        self.assertEqual(armed["mode"], "GRAB")
        # Arming again is idempotent.
        status, armed_again = self.call("POST", "/alum/grab/arm")
        self.assertEqual(armed_again["mode"], "GRAB")

    def test_null_sensor_value_means_sensor_lost(self) -> None:
        self.calibrate(10.0)
        self.call(
            "POST",
            "/alum/tick",
            {"flow": 1000, "turbidity": 500, "temperature": 10, "epoch": self.epoch + 60},
        )
        status, held = self.call(
            "POST",
            "/alum/tick",
            {"flow": None, "turbidity": 500, "temperature": 10, "epoch": self.epoch + 120},
        )
        self.assertEqual(status, 200)
        self.assertTrue(held["pump"]["frozen"])

    def test_audit_chain_and_verify(self) -> None:
        self.calibrate(10.0)
        self.call(
            "POST",
            "/alum/tick",
            {"flow": 1000, "turbidity": 30, "temperature": 5, "epoch": self.epoch + 60},
        )
        status, audit = self.call("GET", "/alum/audit")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(audit["count"], 5)
        self.assertTrue(audit["verification"]["ok"])
        status, verify = self.call("GET", "/alum/audit/verify")
        self.assertTrue(verify["ok"])

    def test_snapshot_report_and_checks_include_alum(self) -> None:
        status, snapshot = self.call("GET", "/snapshot")
        self.assertIn("alum", snapshot)
        status, report = self.call("GET", "/report")
        self.assertIn("alum dosing panel", report)
        status, checks = self.call("GET", "/health/checks")
        names = {row["name"] for row in checks["checks"]}
        self.assertIn("alum_concentration", names)
        self.assertIn("alum_audit", names)
        status, panel = self.call("GET", "/alum/panel")
        self.assertEqual(status, 200)
        self.assertIn("alum dosing panel", panel)

    def test_config_update_validated_and_persisted(self) -> None:
        status, payload = self.call("PUT", "/alum/config", {"grab_enter_ntu": 333.0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["grab_enter_ntu"], 333.0)
        status, bad = self.call("PUT", "/alum/config", {"shift_hours": 99})
        self.assertEqual(status, 400)
        self.assertFalse(bad["ok"])
        status, fetched = self.call("GET", "/alum/config")
        self.assertEqual(fetched["grab_enter_ntu"], 333.0)

    def test_unknown_ticket_is_a_400(self) -> None:
        status, payload = self.call(
            "POST", "/alum/recover", {"ticket_id": "missing"}
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown", payload["error"])

    def test_missing_numeric_field_is_a_400(self) -> None:
        status, payload = self.call("POST", "/alum/tick", {"flow": "fast"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
