"""Time source shared by the alum (coagulant) dosing subsystem.

Production code asks a clock for "now" instead of calling :mod:`time`
directly, so the calibration/decision sequence can be driven deterministically
by the test suite and the offline demo.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """Anything that can report the current time in whole seconds."""

    def now_epoch(self) -> float:
        """Seconds since 1970-01-01 UTC, may carry a fractional part."""

    def iso(self, epoch: float | None = None) -> str:
        """Render an epoch (defaulting to now) as an ISO-8601 UTC string."""


def to_iso(epoch: float) -> str:
    """Stable UTC rendering used inside audit rows and JSON payloads."""

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SystemClock:
    """Real wall clock used by the running service."""

    def now_epoch(self) -> float:
        return time.time()

    def iso(self, epoch: float | None = None) -> str:
        return to_iso(time.time() if epoch is None else epoch)


class FakeClock:
    """Deterministic clock whose time only moves when the test advances it."""

    def __init__(self, epoch: float = 0.0) -> None:
        self._epoch = float(epoch)

    def now_epoch(self) -> float:
        return self._epoch

    def set(self, epoch: float) -> None:
        self._epoch = float(epoch)

    def advance(self, seconds: float) -> float:
        self._epoch += float(seconds)
        return self._epoch

    def iso(self, epoch: float | None = None) -> str:
        return to_iso(self._epoch if epoch is None else epoch)
