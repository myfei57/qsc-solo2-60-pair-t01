"""Shift calibrated alum solution strength.

The confirmed snapshot lives under a single key so every dose reads one
consistent (value, epoch, shift) triple: a refresh bumps the epoch and every
dose decided afterwards must follow the refreshed basis. A calibration whose
reading is lost leaves a pending record behind; recovering it is idempotent
because the recovery marker is keyed by the deterministic calibration id.
"""

from __future__ import annotations

import json
import time

from waterplant.audit import Auditor
from waterplant.store.store import Store

from .model import DEFAULT_CONCENTRATION, validate_concentration
from .report import CalibrationOutcome, ConcentrationState, RecoveryOutcome

CONCENTRATION_KEY = "alum:concentration"
PENDING_KEY = "alum:pending"
LAST_READING_KEY = "alum:last_reading"
RECOVERY_KEY_PREFIX = "alum:recovery:"

AUDIT_CALIBRATION = "alum-calibration"
AUDIT_LOST = "alum-calibration-lost"
AUDIT_RECOVERY = "alum-recovery"


def _default_snapshot() -> dict[str, object]:
    return {
        "value": DEFAULT_CONCENTRATION,
        "previous": DEFAULT_CONCENTRATION,
        "epoch": 0,
        "shift": "",
    }


class ConcentrationLedger:
    """Confirmed solution strength plus one pending recalibration."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._auditor = Auditor(store)

    def ensure_default(self) -> None:
        """Seed a usable snapshot on first boot."""

        if not self._store.get(CONCENTRATION_KEY)[1]:
            self._write_snapshot(DEFAULT_CONCENTRATION, DEFAULT_CONCENTRATION, 1, "seed")

    def _snapshot(self) -> dict[str, object]:
        raw, present = self._store.get(CONCENTRATION_KEY)
        if present:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                snapshot = _default_snapshot()
                for key in snapshot:
                    if key in payload:
                        snapshot[key] = payload[key]
                snapshot["value"] = float(snapshot["value"])
                snapshot["previous"] = float(snapshot["previous"])
                snapshot["epoch"] = int(snapshot["epoch"])
                snapshot["shift"] = str(snapshot["shift"])
                return snapshot
        return _default_snapshot()

    def _write_snapshot(self, value: float, previous: float, epoch: int, shift: str) -> None:
        self._store.put(
            CONCENTRATION_KEY,
            json.dumps(
                {"value": value, "previous": previous, "epoch": epoch, "shift": shift},
                ensure_ascii=False,
            ),
        )

    def _pending(self) -> dict[str, object] | None:
        raw, present = self._store.get(PENDING_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict) or "calibration_id" not in payload:
            return None
        return payload

    def _last_reading(self) -> tuple[float, bool]:
        raw, present = self._store.get(LAST_READING_KEY)
        if not present:
            return 0.0, False
        try:
            return float(raw), True
        except ValueError:
            return 0.0, False

    def current(self) -> ConcentrationState:
        snapshot = self._snapshot()
        pending = self._pending()
        return ConcentrationState(
            value=float(snapshot["value"]),
            previous=float(snapshot["previous"]),
            epoch=int(snapshot["epoch"]),
            shift=str(snapshot["shift"]),
            pending=pending is not None,
            pending_shift="" if pending is None else str(pending.get("shift", "")),
        )

    def calibrate(self, shift: str, reading: float | None) -> CalibrationOutcome:
        """Recalibrate for a shift; a missing reading leaves a pending gap."""

        snapshot = self._snapshot()
        calibration_id = f"cal-{shift}"
        if reading is None:
            existing = self._pending()
            if existing is not None and existing.get("calibration_id") == calibration_id:
                return CalibrationOutcome(
                    status="pending",
                    shift=shift,
                    value=float(snapshot["value"]),
                    previous=float(snapshot["previous"]),
                    epoch=int(snapshot["epoch"]),
                    calibration_id=calibration_id,
                    note="already-pending",
                )
            self._store.put(
                PENDING_KEY,
                json.dumps(
                    {
                        "calibration_id": calibration_id,
                        "shift": shift,
                        "base_epoch": int(snapshot["epoch"]),
                        "requested_at": int(time.time()),
                    },
                    ensure_ascii=False,
                ),
            )
            self._auditor.record(
                AUDIT_LOST,
                json.dumps(
                    {
                        "shift": shift,
                        "calibration_id": calibration_id,
                        "base_epoch": int(snapshot["epoch"]),
                    },
                    ensure_ascii=False,
                ),
            )
            return CalibrationOutcome(
                status="pending",
                shift=shift,
                value=float(snapshot["value"]),
                previous=float(snapshot["previous"]),
                epoch=int(snapshot["epoch"]),
                calibration_id=calibration_id,
                note="reading-lost",
            )
        validate_concentration(reading)
        epoch = int(snapshot["epoch"]) + 1
        previous = float(snapshot["value"])
        self._store.put(LAST_READING_KEY, f"{reading:.4f}")
        self._write_snapshot(reading, previous, epoch, shift)
        self._store.delete(PENDING_KEY)
        self._auditor.record(
            AUDIT_CALIBRATION,
            json.dumps(
                {
                    "shift": shift,
                    "value": reading,
                    "previous": previous,
                    "epoch": epoch,
                    "calibration_id": calibration_id,
                },
                ensure_ascii=False,
            ),
        )
        return CalibrationOutcome(
            status="confirmed",
            shift=shift,
            value=reading,
            previous=previous,
            epoch=epoch,
            calibration_id=calibration_id,
            note="",
        )

    def recover(self, reading: float | None = None) -> RecoveryOutcome:
        """Complete a calibration whose reading was lost; safe to repeat.

        The first call applies the recovery and records a marker keyed by the
        calibration id. Later calls for the same calibration - including a
        pending record recreated by a retried loss report - return the stored
        outcome without touching the snapshot or the audit trail again.
        """

        pending = self._pending()
        snapshot = self._snapshot()
        if pending is None:
            return RecoveryOutcome(
                recovered=False,
                changed=False,
                reason="no-pending-calibration",
                value=float(snapshot["value"]),
                source="none",
                epoch=int(snapshot["epoch"]),
                calibration_id="",
            )
        calibration_id = str(pending["calibration_id"])
        marker, marker_present = self._store.get(f"{RECOVERY_KEY_PREFIX}{calibration_id}")
        if marker_present:
            try:
                stored = json.loads(marker)
            except ValueError:
                stored = None
            if isinstance(stored, dict):
                self._store.delete(PENDING_KEY)
                stored["changed"] = False
                return RecoveryOutcome(**stored)
        if reading is not None:
            validate_concentration(reading)
            value, source = reading, "resubmitted"
        else:
            last, last_present = self._last_reading()
            if last_present:
                value, source = last, "last-reading"
            else:
                value, source = DEFAULT_CONCENTRATION, "default"
        epoch = int(snapshot["epoch"]) + 1
        previous = float(snapshot["value"])
        self._write_snapshot(value, previous, epoch, str(pending.get("shift", "")))
        self._store.delete(PENDING_KEY)
        outcome = RecoveryOutcome(
            recovered=True,
            changed=True,
            reason="recovered",
            value=value,
            source=source,
            epoch=epoch,
            calibration_id=calibration_id,
        )
        self._store.put(
            f"{RECOVERY_KEY_PREFIX}{calibration_id}",
            json.dumps(outcome.as_dict(), ensure_ascii=False),
        )
        self._auditor.record(AUDIT_RECOVERY, json.dumps(outcome.as_dict(), ensure_ascii=False))
        return outcome

    def describe(self) -> str:
        state = self.current()
        return (
            f"alum concentration={state.value:.4f} epoch={state.epoch} "
            f"pending={state.pending}"
        )
