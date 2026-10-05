"""Shift-wise alum solution concentration calibration.

The concentration of a freshly prepared alum solution is re-measured every
shift (titration / density readings).  A calibration runs as an explicit
ticket so that losing one replicate reading is a *partial failure* that can be
recovered instead of poisoning the active strength:

    OPEN -> READING_LOST -> (re-sample) -> ACCEPTED -> (next calibration)
      \-------> ACCEPTED        \--> REJECTED

Only an ACCEPTED calibration produces a new, versioned concentration.  The
version is written together with its CALIB_ACCEPTED audit row in one store
transaction, and dosing always reads concentrations "as of" the decision
time: a reading taken during a tick can never change the strength that tick
was computed with.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum

from waterplant.store.store import Store

from .clock import Clock, SystemClock, to_iso
from .model import AlumConfig

TICKETS_KEY = "coag:alum:tickets"
VERSIONS_KEY = "coag:alum:versions"
ACTIVE_TICKET_KEY = "coag:alum:active_ticket"
ACTIVE_VERSION_KEY = "coag:alum:active_version"


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    READING_LOST = "READING_LOST"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class Situation(str, Enum):
    """What the dosing decision can rely on right now."""

    FRESH = "FRESH"
    PENDING = "PENDING"
    STALE = "STALE"
    NONE = "NONE"


@dataclass
class Reading:
    """One replicate concentration measurement for a ticket."""

    value_pct: float
    at_epoch: float
    replicate: int

    def as_dict(self) -> dict[str, object]:
        return {
            "value_pct": self.value_pct,
            "at_epoch": self.at_epoch,
            "at": to_iso(self.at_epoch),
            "replicate": self.replicate,
        }


@dataclass
class CalibTicket:
    """One shift's calibration attempt."""

    id: str
    shift_id: str
    shift_start_epoch: float
    opened_at: float
    due_epoch: float
    status: str = TicketStatus.OPEN.value
    readings: list[Reading] = field(default_factory=list)
    lost_replicates: list[int] = field(default_factory=list)
    recovery_count: int = 0
    concentration_pct: float | None = None
    closed_at: float | None = None
    version_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (TicketStatus.OPEN.value, TicketStatus.READING_LOST.value)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "shift_id": self.shift_id,
            "shift_start_epoch": self.shift_start_epoch,
            "shift_start": to_iso(self.shift_start_epoch),
            "opened_at": self.opened_at,
            "opened": to_iso(self.opened_at),
            "due_epoch": self.due_epoch,
            "due": to_iso(self.due_epoch),
            "status": self.status,
            "readings": [reading.as_dict() for reading in self.readings],
            "lost_replicates": list(self.lost_replicates),
            "recovery_count": self.recovery_count,
            "concentration_pct": self.concentration_pct,
            "closed_at": self.closed_at,
            "closed": to_iso(self.closed_at) if self.closed_at is not None else None,
            "version_id": self.version_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CalibTicket":
        readings = [
            Reading(
                value_pct=float(item["value_pct"]),
                at_epoch=float(item["at_epoch"]),
                replicate=int(item["replicate"]),
            )
            for item in payload.get("readings", [])  # type: ignore[arg-type]
        ]
        concentration = payload.get("concentration_pct")
        return cls(
            id=str(payload["id"]),
            shift_id=str(payload["shift_id"]),
            shift_start_epoch=float(payload["shift_start_epoch"]),
            opened_at=float(payload["opened_at"]),
            due_epoch=float(payload["due_epoch"]),
            status=str(payload.get("status", TicketStatus.OPEN.value)),
            readings=readings,
            lost_replicates=[int(item) for item in payload.get("lost_replicates", [])],  # type: ignore[arg-type]
            recovery_count=int(payload.get("recovery_count", 0)),
            concentration_pct=None if concentration is None else float(concentration),
            closed_at=(
                None if payload.get("closed_at") is None else float(payload["closed_at"])  # type: ignore[arg-type]
            ),
            version_id=payload.get("version_id"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ConcentrationVersion:
    """One accepted concentration, immutable once applied."""

    id: str
    concentration_pct: float
    applied_at: float
    shift_id: str
    ticket_id: str
    prior_version_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "concentration_pct": self.concentration_pct,
            "applied_at": self.applied_at,
            "applied": to_iso(self.applied_at),
            "shift_id": self.shift_id,
            "ticket_id": self.ticket_id,
            "prior_version_id": self.prior_version_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ConcentrationVersion":
        return cls(
            id=str(payload["id"]),
            concentration_pct=float(payload["concentration_pct"]),
            applied_at=float(payload["applied_at"]),
            shift_id=str(payload["shift_id"]),
            ticket_id=str(payload["ticket_id"]),
            prior_version_id=payload.get("prior_version_id"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ConcentrationView:
    """The single strength a decision is allowed to use at one instant."""

    situation: Situation
    concentration_pct: float
    version: ConcentrationVersion | None
    active_ticket: CalibTicket | None
    previous_version: ConcentrationVersion | None
    age_seconds: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "situation": self.situation.value,
            "concentration_pct": self.concentration_pct,
            "version_id": None if self.version is None else self.version.id,
            "applied_at": None if self.version is None else self.version.applied_at,
            "active_ticket_id": None if self.active_ticket is None else self.active_ticket.id,
            "ticket_status": None if self.active_ticket is None else self.active_ticket.status,
            "previous_version_id": (
                None if self.previous_version is None else self.previous_version.id
            ),
            "age_seconds": self.age_seconds,
            "reasons": list(self.reasons),
        }


def shift_start_for(epoch: float, shift_hours: int) -> float:
    """Align an epoch down to its shift boundary (UTC shifts by default)."""

    length = float(shift_hours) * 3600.0
    return (epoch // length) * length


class CalibrationService:
    """Owns calibration tickets and the immutable concentration timeline."""

    def __init__(
        self,
        store: Store,
        config: AlumConfig,
        clock: Clock | None = None,
        audit_sink: "object | None" = None,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock or SystemClock()
        self._audit_sink = audit_sink

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def config(self) -> AlumConfig:
        return self._config

    # -- persistence -------------------------------------------------------

    def _load_tickets(self) -> list[CalibTicket]:
        raw, present = self._store.get(TICKETS_KEY)
        if not present:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        tickets = []
        for item in payload:
            if isinstance(item, dict):
                try:
                    tickets.append(CalibTicket.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
        return tickets

    def _save_tickets(self, tickets: list[CalibTicket]) -> None:
        self._store.put(
            TICKETS_KEY,
            json.dumps([ticket.as_dict() for ticket in tickets], ensure_ascii=False),
        )

    def _load_versions(self) -> list[ConcentrationVersion]:
        raw, present = self._store.get(VERSIONS_KEY)
        if not present:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        versions = []
        for item in payload:
            if isinstance(item, dict):
                try:
                    versions.append(ConcentrationVersion.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
        return versions

    def _save_versions(self, versions: list[ConcentrationVersion]) -> None:
        self._store.put(
            VERSIONS_KEY,
            json.dumps([version.as_dict() for version in versions], ensure_ascii=False),
        )

    def _active_ticket_id(self) -> str | None:
        raw, present = self._store.get(ACTIVE_TICKET_KEY)
        return raw or None if present else None

    def _set_active_ticket_id(self, ticket_id: str | None) -> None:
        if ticket_id is None:
            self._store.delete(ACTIVE_TICKET_KEY)
        else:
            self._store.put(ACTIVE_TICKET_KEY, ticket_id)

    # -- queries -----------------------------------------------------------

    def shift_id(self, epoch: float | None = None) -> str:
        epoch = self._clock.now_epoch() if epoch is None else epoch
        start = shift_start_for(epoch, self._config.shift_hours)
        day_start = start - (int(start // 3600) % 24) * 3600
        within_day = int((start - day_start) // 3600) // self._config.shift_hours
        return to_iso(start)[:10].replace("-", "") + f"-S{within_day + 1}"

    def tickets(self) -> list[CalibTicket]:
        return self._load_tickets()

    def ticket(self, ticket_id: str) -> CalibTicket | None:
        return next(
            (ticket for ticket in self._load_tickets() if ticket.id == ticket_id), None
        )

    def active_ticket(self) -> CalibTicket | None:
        active_id = self._active_ticket_id()
        if active_id is None:
            return None
        return self.ticket(active_id)

    def versions(self) -> list[ConcentrationVersion]:
        return self._load_versions()

    def version_as_of(self, epoch: float) -> ConcentrationVersion | None:
        """Latest version applied at or before ``epoch`` (the as-of read)."""

        applicable = [
            version
            for version in self._load_versions()
            if version.applied_at <= epoch
        ]
        return applicable[-1] if applicable else None

    def latest_version(self) -> ConcentrationVersion | None:
        versions = self._load_versions()
        return versions[-1] if versions else None

    def situation(self, epoch: float | None = None) -> ConcentrationView:
        """Resolve the concentration basis plus why that basis is in force."""

        now = self._clock.now_epoch() if epoch is None else epoch
        version = self.version_as_of(now)
        ticket = self.active_ticket()
        reasons: list[str] = []

        if version is None:
            reasons.append("NO_ACCEPTED_VERSION")
            return ConcentrationView(
                situation=Situation.NONE,
                concentration_pct=self._config.floor_concentration_pct,
                version=None,
                active_ticket=ticket,
                previous_version=None,
                age_seconds=0.0,
                reasons=tuple(reasons),
            )

        age = now - version.applied_at
        previous = None
        all_versions = self._load_versions()
        if len(all_versions) >= 2:
            previous = all_versions[-2]

        age = now - version.applied_at
        previous = None
        all_versions = self._load_versions()
        if len(all_versions) >= 2:
            previous = all_versions[-2]

        # A version past its staleness window is not a trustworthy basis even
        # if the shift's re-calibration is still in flight: report STALE so the
        # policy matrix takes the conservative floor path.
        if age > self._config.staleness_seconds:
            reasons.append("VERSION_PAST_STALENESS")
            if ticket is not None and ticket.is_open:
                reasons.append(f"TICKET_{ticket.status}")
            return ConcentrationView(
                situation=Situation.STALE,
                concentration_pct=min(version.concentration_pct, self._config.floor_concentration_pct),
                version=version,
                active_ticket=ticket,
                previous_version=previous,
                age_seconds=age,
                reasons=tuple(reasons),
            )

        if ticket is not None and ticket.is_open:
            reasons.append(f"TICKET_{ticket.status}")
            return ConcentrationView(
                situation=Situation.PENDING,
                concentration_pct=version.concentration_pct,
                version=version,
                active_ticket=ticket,
                previous_version=previous,
                age_seconds=age,
                reasons=tuple(reasons + ["PENDING_KEEPS_OLD_UNTIL_ACCEPTED"]),
            )

        reasons.append("VERSION_FRESH")
        return ConcentrationView(
            situation=Situation.FRESH,
            concentration_pct=version.concentration_pct,
            version=version,
            active_ticket=ticket,
            previous_version=previous,
            age_seconds=age,
            reasons=tuple(reasons),
        )

    # -- ticket lifecycle --------------------------------------------------

    def open_ticket(self, epoch: float | None = None) -> tuple[CalibTicket, list[object]]:
        """Open the shift ticket; exactly one open ticket per shift."""

        now = self._clock.now_epoch() if epoch is None else epoch
        events: list[object] = []
        with self._store.transaction():
            active = self.active_ticket()
            if active is not None and active.is_open:
                return active, events
            shift_start = shift_start_for(now, self._config.shift_hours)
            ticket = CalibTicket(
                id=uuid.uuid4().hex[:12],
                shift_id=self.shift_id(now),
                shift_start_epoch=shift_start,
                opened_at=now,
                due_epoch=shift_start + self._config.max_shift_seconds,
            )
            tickets = self._load_tickets()
            tickets.append(ticket)
            self._save_tickets(tickets)
            self._set_active_ticket_id(ticket.id)
            events.append(self._event(ticket, "CALIB_OPENED", now, {"due_epoch": ticket.due_epoch}))
        return ticket, events

    def maintain(self, epoch: float | None = None) -> list[object]:
        """Roll the calibration schedule forward for one decision instant."""

        now = self._clock.now_epoch() if epoch is None else epoch
        events: list[object] = []
        with self._store.transaction():
            active = self.active_ticket()
            if active is not None and active.is_open:
                if now > active.due_epoch and active.status == TicketStatus.OPEN.value:
                    active.status = TicketStatus.READING_LOST.value
                    self._upsert_ticket(active)
                    events.append(
                        self._event(active, "CALIB_OVERDUE", now, {"due_epoch": active.due_epoch})
                    )
                return events

            shift_start = shift_start_for(now, self._config.shift_hours)
            tickets = self._load_tickets()
            latest = tickets[-1] if tickets else None
            if (
                latest is not None
                and latest.shift_start_epoch == shift_start
                and latest.status == TicketStatus.ACCEPTED.value
            ):
                # This shift was already calibrated; no new ticket until rollover.
                return events
            _, open_events = self.open_ticket(now)
            events.extend(open_events)
        return events

    def submit_reading(
        self,
        ticket_id: str,
        replicate: int,
        value_pct: float,
        epoch: float | None = None,
    ) -> tuple[CalibTicket, list[object]]:
        """Record one replicate; completing the set accepts atomically."""

        now = self._clock.now_epoch() if epoch is None else epoch
        with self._store.transaction():
            ticket = self._require_ticket(ticket_id)
            events: list[object] = []
            if not ticket.is_open:
                return ticket, []
            if replicate < 1 or replicate > self._config.required_readings:
                raise ValueError(
                    f"replicate must lie in 1..{self._config.required_readings}"
                )
            existing = next(
                (reading for reading in ticket.readings if reading.replicate == replicate),
                None,
            )
            if existing is not None:
                # Idempotent replay: same reading, same ticket, no new state.
                return ticket, []
            ticket.readings.append(
                Reading(value_pct=float(value_pct), at_epoch=now, replicate=replicate)
            )
            ticket.readings.sort(key=lambda reading: reading.replicate)
            ticket.lost_replicates = [
                number for number in ticket.lost_replicates if number != replicate
            ]
            events.append(
                self._event(
                    ticket,
                    "CALIB_READING",
                    now,
                    {"replicate": replicate, "value_pct": float(value_pct)},
                )
            )
            if len(ticket.readings) >= self._config.required_readings:
                accept_events = self._accept_locked(ticket, now)
                events.extend(accept_events)
            else:
                self._upsert_ticket(ticket)
        return ticket, events

    def report_lost_reading(
        self, ticket_id: str, replicate: int, epoch: float | None = None
    ) -> tuple[CalibTicket, list[object]]:
        """Mark a failed replicate: the partial failure the recovery path fixes."""

        now = self._clock.now_epoch() if epoch is None else epoch
        with self._store.transaction():
            ticket = self._require_ticket(ticket_id)
            events: list[object] = []
            if not ticket.is_open:
                return ticket, []
            if replicate < 1 or replicate > self._config.required_readings:
                raise ValueError(
                    f"replicate must lie in 1..{self._config.required_readings}"
                )
            if any(reading.replicate == replicate for reading in ticket.readings):
                return ticket, []
            if replicate in ticket.lost_replicates:
                return ticket, []
            ticket.lost_replicates.append(replicate)
            ticket.lost_replicates.sort()
            ticket.status = TicketStatus.READING_LOST.value
            self._upsert_ticket(ticket)
            events.append(self._event(ticket, "CALIB_READING_LOST", now, {"replicate": replicate}))
        return ticket, events

    def recover(
        self, ticket_id: str, epoch: float | None = None
    ) -> tuple[CalibTicket, list[object]]:
        """Retry the failed replicates for a ticket.

        Idempotent by design: recovering an ACCEPTED ticket returns it with no
        new events, and recovering twice in READING_LOST only bumps the counter
        once per distinct recovery round.
        """

        now = self._clock.now_epoch() if epoch is None else epoch
        with self._store.transaction():
            ticket = self._require_ticket(ticket_id)
            if ticket.status == TicketStatus.ACCEPTED.value:
                return ticket, []
            if not ticket.lost_replicates:
                return ticket, []
            events = [self._event(ticket, "CALIB_RECOVERY", now, {"round": ticket.recovery_count + 1})]
            ticket.recovery_count += 1
            ticket.status = TicketStatus.OPEN.value
            ticket.lost_replicates = []
            self._upsert_ticket(ticket)
        return ticket, events

    def reject(
        self, ticket_id: str, reason: str, epoch: float | None = None
    ) -> tuple[CalibTicket, list[object]]:
        """Reject an out-of-band calibration without touching the live strength."""

        now = self._clock.now_epoch() if epoch is None else epoch
        with self._store.transaction():
            ticket = self._require_ticket(ticket_id)
            if ticket.status == TicketStatus.REJECTED.value:
                return ticket, []
            ticket.status = TicketStatus.REJECTED.value
            ticket.closed_at = now
            self._upsert_ticket(ticket)
            if self._active_ticket_id() == ticket.id:
                self._set_active_ticket_id(None)
            events = [self._event(ticket, "CALIB_REJECTED", now, {"reason": reason})]
        return ticket, events

    # -- internals ---------------------------------------------------------

    def _accept_locked(self, ticket: CalibTicket, now: float) -> list[object]:
        values = [reading.value_pct for reading in ticket.readings]
        concentration = sum(values) / len(values)
        events: list[object] = []
        if not (
            self._config.concentration_min_pct
            <= concentration
            <= self._config.concentration_max_pct
        ):
            ticket.status = TicketStatus.REJECTED.value
            ticket.closed_at = now
            self._upsert_ticket(ticket)
            if self._active_ticket_id() == ticket.id:
                self._set_active_ticket_id(None)
            events.append(
                self._event(
                    ticket,
                    "CALIB_REJECTED",
                    now,
                    {
                        "reason": "OUT_OF_SANITY_BAND",
                        "concentration_pct": concentration,
                        "min_pct": self._config.concentration_min_pct,
                        "max_pct": self._config.concentration_max_pct,
                    },
                )
            )
            return events

        versions = self._load_versions()
        prior = versions[-1] if versions else None
        version = ConcentrationVersion(
            id=uuid.uuid4().hex[:12],
            concentration_pct=concentration,
            applied_at=now,
            shift_id=ticket.shift_id,
            ticket_id=ticket.id,
            prior_version_id=None if prior is None else prior.id,
        )
        # Atomic basis refresh: the version, the ticket close-out and the
        # CALIB_ACCEPTED audit event commit in the same store transaction, so a
        # decision can never see the new strength without its audit evidence.
        versions.append(version)
        self._save_versions(versions)
        ticket.status = TicketStatus.ACCEPTED.value
        ticket.concentration_pct = concentration
        ticket.closed_at = now
        ticket.version_id = version.id
        self._upsert_ticket(ticket)
        self._set_active_ticket_id(None)
        events.append(
            self._event(
                ticket,
                "CALIB_ACCEPTED",
                now,
                {
                    "version_id": version.id,
                    "concentration_pct": concentration,
                    "prior_version_id": version.prior_version_id,
                    "reading_count": len(values),
                },
            )
        )
        return events

    def _require_ticket(self, ticket_id: str) -> CalibTicket:
        ticket = self.ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"unknown calibration ticket {ticket_id}")
        return ticket

    def _upsert_ticket(self, ticket: CalibTicket) -> None:
        tickets = self._load_tickets()
        for index, existing in enumerate(tickets):
            if existing.id == ticket.id:
                tickets[index] = ticket
                break
        else:
            tickets.append(ticket)
        self._save_tickets(tickets)

    def _event(
        self, ticket: CalibTicket, kind: str, epoch: float, payload: dict[str, object]
    ) -> object:
        row = {
            "kind": kind,
            "at_epoch": epoch,
            "at": to_iso(epoch),
            "ticket_id": ticket.id,
            "shift_id": ticket.shift_id,
            "idempotency_key": f"{kind}:{ticket.id}:{_stable(payload)}",
            "payload": payload,
        }
        if self._audit_sink is not None:
            self._audit_sink.append_calib(row)  # type: ignore[attr-defined]
        return row


def _stable(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
