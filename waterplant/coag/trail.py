"""Hash-chained audit trail for alum dosing.

Every dosing decision and every calibration lifecycle event is appended to
one ordered trail.  Each row carries the SHA-256 of the previous row plus a
sequence number, so a missing, duplicated or modified record is detectable
when the console or an auditor verifies the chain.

Appends are idempotent through an explicit ``idempotency_key``: replaying the
same aligned tick, or re-triggering a recovery event after a retry, cannot
write the row twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from waterplant.store.store import Store

from .clock import to_iso

TRAIL_KEY = "coag:alum:audit"
GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditRow:
    """One tamper-evident trail entry."""

    seq: int
    kind: str
    at_epoch: float
    idempotency_key: str
    payload: dict[str, object]
    prev_hash: str
    row_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at_epoch": self.at_epoch,
            "at": to_iso(self.at_epoch),
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.row_hash,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "AuditRow":
        return cls(
            seq=int(item["seq"]),
            kind=str(item["kind"]),
            at_epoch=float(item["at_epoch"]),
            idempotency_key=str(item["idempotency_key"]),
            payload=dict(item.get("payload", {})),  # type: ignore[arg-type]
            prev_hash=str(item["prev_hash"]),
            row_hash=str(item["hash"]),
        )


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking the trail from the genesis row."""

    rows: int
    ok: bool
    broken_at_seq: int | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "ok": self.ok,
            "broken_at_seq": self.broken_at_seq,
            "reason": self.reason,
        }


def canonical(payload: dict[str, object]) -> str:
    """Canonical JSON form used to hash trail rows."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_hash(
    seq: int,
    kind: str,
    at_epoch: float,
    idempotency_key: str,
    payload: dict[str, object],
    prev_hash: str,
) -> str:
    body = canonical(
        {
            "seq": seq,
            "kind": kind,
            "at_epoch": at_epoch,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AuditTrail:
    """Reads, appends and verifies the alum audit chain in the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def rows(self) -> list[AuditRow]:
        raw, present = self._store.get(TRAIL_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        rows = []
        for item in parsed:
            if isinstance(item, dict):
                try:
                    rows.append(AuditRow.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
        return rows

    def last(self) -> AuditRow | None:
        rows = self.rows()
        return rows[-1] if rows else None

    def find(self, idempotency_key: str) -> AuditRow | None:
        return next(
            (row for row in self.rows() if row.idempotency_key == idempotency_key),
            None,
        )

    def append(
        self,
        kind: str,
        at_epoch: float,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> AuditRow:
        """Append one row, skipping a replay that carries the same key."""

        existing = self.find(idempotency_key)
        if existing is not None:
            return existing
        rows = self.rows()
        seq = len(rows) + 1
        prev_hash = rows[-1].row_hash if rows else GENESIS_HASH
        digest = row_hash(seq, kind, at_epoch, idempotency_key, payload, prev_hash)
        row = AuditRow(
            seq=seq,
            kind=kind,
            at_epoch=at_epoch,
            idempotency_key=idempotency_key,
            payload=payload,
            prev_hash=prev_hash,
            row_hash=digest,
        )
        raw_rows = [stored.as_dict() for stored in rows]
        raw_rows.append(row.as_dict())
        self._store.put(TRAIL_KEY, json.dumps(raw_rows, ensure_ascii=False))
        return row

    def append_calib(self, event: dict[str, object]) -> AuditRow:
        """Persist an event produced by the calibration service."""

        payload = dict(event["payload"])  # type: ignore[arg-type]
        return self.append(
            kind=str(event["kind"]),
            at_epoch=float(event["at_epoch"]),  # type: ignore[arg-type]
            idempotency_key=str(event["idempotency_key"]),
            payload={
                "ticket_id": event["ticket_id"],
                "shift_id": event["shift_id"],
                "detail": payload,
            },
        )

    def verify(self) -> ChainVerification:
        rows = self.rows()
        previous = GENESIS_HASH
        for expected_seq, row in enumerate(rows, start=1):
            if row.seq != expected_seq:
                return ChainVerification(
                    rows=len(rows), ok=False, broken_at_seq=expected_seq, reason="gap in sequence"
                )
            if row.prev_hash != previous:
                return ChainVerification(
                    rows=len(rows),
                    ok=False,
                    broken_at_seq=row.seq,
                    reason="previous hash does not chain",
                )
            digest = row_hash(
                row.seq,
                row.kind,
                row.at_epoch,
                row.idempotency_key,
                row.payload,
                row.prev_hash,
            )
            if digest != row.row_hash:
                return ChainVerification(
                    rows=len(rows),
                    ok=False,
                    broken_at_seq=row.seq,
                    reason="row payload does not match its hash",
                )
            previous = row.row_hash
        return ChainVerification(rows=len(rows), ok=True, broken_at_seq=None, reason="chain intact")
