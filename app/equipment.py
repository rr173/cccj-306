"""Shared equipment room: physical items, suspensions and class reservations.

Domain model
------------
* A physical item (``equipment_item``) is registered by its owning school and
  carries capability tags. The owner may add a suspension window and lift it
  early.
* A reservation belongs to one organizer school *for one of its own training
  classes* and lists requirements: each requirement is a count plus a set of
  tags. Every physical item handed out for that requirement must carry all of
  the requirement's tags.
* Reservation time intervals are half-open ``[start, end)``. A reservation that
  has not been picked up is only evaluated on its remaining window
  ``[max(start, now), end)``; elapsed time is never retroactively required.
* Reservations are accepted in first-come order (``seq``). The organizer may
  raise a reservation only if the whole set is feasible at that moment. When
  the currently accepted set cannot all hold, the first reservation with a
  complete feasible plan keeps its plan; each later reservation is then kept
  only if it remains jointly feasible with those already kept. Keeping a
  reservation may re-plan other not-yet-picked reservations; an item that is
  merely planned for another booking does not by itself make the new one
  infeasible.
* An accepted reservation that cannot currently be kept is shown as short
  (缺套), holds no items, and is restored in its original priority position as
  soon as handovers are returned or suspensions lifted. It never has to be
  re-ordered.
* Picked-up reservations are fixed handovers: their item list never changes
  automatically. They hold their items until actually returned; an overdue
  pickup simply reports no projected release time instead of blocking forever.

Concurrency
-----------
Every read and write runs inside ``BEGIN IMMEDIATE`` so all reservation
decisions and handovers are serialized by the SQLite write lock. A partial
unique index on ``equipment_allocation(item_id) WHERE picked_up_at IS NOT NULL
AND returned_at IS NULL`` is the database-level guard against lending one
physical item twice.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .models import (
    EquipmentItemIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentSuspensionIn,
    EquipmentTagsIn,
)
from .services import ServiceError, write_transaction

ACCEPTED = "accepted"
PICKED_UP = "picked_up"
RETURNED = "returned"
CANCELLED = "cancelled"
EXPIRED = "expired"

STATUS_LABELS_ZH = {
    ACCEPTED: "已受理",
    PICKED_UP: "已领出",
    RETURNED: "已归还",
    CANCELLED: "已取消",
    EXPIRED: "已过期",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


_clock: Optional[Callable[[], datetime]] = None


def set_clock(clock: Optional[Callable[[], datetime]]) -> None:
    """Install a deterministic clock for demos/tests; ``None`` restores real time."""
    global _clock
    _clock = clock


def current_time() -> datetime:
    if _clock is not None:
        return _clock()
    return datetime.now(timezone.utc)


def current_iso() -> str:
    return current_time().isoformat()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    value = datetime.fromisoformat(ts)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open interval overlap: ``[a_start, a_end)`` vs ``[b_start, b_end)``."""
    return a_start < b_end and b_start < a_end


def _reservation_window(row: sqlite3.Row, now: datetime) -> tuple[datetime, datetime]:
    """Effective window for planning.

    Picked-up handovers use their full promised interval. A booking that has
    not been handed over only needs its remaining time, so the part before
    ``now`` is not required again.
    """
    start = _parse(row["start_at"])
    end = _parse(row["end_at"])
    if row["status"] == PICKED_UP:
        return (start, end)
    if now > start:
        return (now, end)
    return (start, end)


def _handover_window(row: sqlite3.Row, now: datetime) -> tuple[datetime, datetime]:
    """Window an actually handed-over item is unavailable for re-planning.

    An overdue loan has no projected release, so it blocks every future window
    until the physical return is recorded.
    """
    start = _parse(row["start_at"])
    end = _parse(row["end_at"])
    if end <= now:
        return (start, datetime.max.replace(tzinfo=timezone.utc))
    return (start, end)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _all_items(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM equipment_item ORDER BY item_id"
    ).fetchall()
    items: dict[str, dict[str, Any]] = {}
    for row in rows:
        tags = [
            tag_row["tag"]
            for tag_row in conn.execute(
                "SELECT tag FROM equipment_item_tag WHERE item_id = ? ORDER BY tag",
                (row["item_id"],),
            ).fetchall()
        ]
        items[row["item_id"]] = {
            "item_id": row["item_id"],
            "owner_school_id": row["owner_school_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "tags": tags,
            "tag_set": set(tags),
        }
    return items


def _suspension_intervals(
    conn: sqlite3.Connection, item_id: Optional[str] = None
) -> dict[str, list[dict[str, Any]]]:
    """Effective (not yet lifted) suspension intervals per item."""
    clauses = ""
    params: list[Any] = []
    if item_id is not None:
        clauses = " WHERE item_id = ?"
        params.append(item_id)
    rows = conn.execute(
        f"SELECT * FROM equipment_suspension{clauses} ORDER BY start_at, suspension_id",
        params,
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        start = _parse(row["start_at"])
        end = _parse(row["end_at"])
        lifted_at = _parse(row["lifted_at"]) if row["lifted_at"] else None
        effective_end = min(end, lifted_at) if lifted_at is not None else end
        if effective_end <= start:
            continue
        result.setdefault(row["item_id"], []).append(
            {
                "suspension_id": row["suspension_id"],
                "item_id": row["item_id"],
                "start": start,
                "end": effective_end,
                "reason": row["reason"],
                "lifted_at": row["lifted_at"],
            }
        )
    return result


def _requirements_for(
    conn: sqlite3.Connection, reservation_row_or_id
) -> list[dict[str, Any]]:
    reservation_id = (
        reservation_row_or_id["reservation_id"]
        if isinstance(reservation_row_or_id, sqlite3.Row)
        else reservation_row_or_id
    )
    rows = conn.execute(
        """
        SELECT * FROM equipment_reservation_requirement
        WHERE reservation_id = ?
        ORDER BY position, requirement_id
        """,
        (reservation_id,),
    ).fetchall()
    requirements = []
    for row in rows:
        tags = [
            tag_row["tag"]
            for tag_row in conn.execute(
                "SELECT tag FROM equipment_requirement_tag WHERE requirement_id = ? ORDER BY tag",
                (row["requirement_id"],),
            ).fetchall()
        ]
        requirements.append(
            {
                "requirement_id": row["requirement_id"],
                "tags": tags,
                "count": row["count"],
            }
        )
    return requirements


def _allocations_for(
    conn: sqlite3.Connection, reservation_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM equipment_allocation
        WHERE reservation_id = ?
        ORDER BY allocation_id
        """,
        (reservation_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Interval-aware item allocation (backtracking, re-plans soft reservations)
# ---------------------------------------------------------------------------


def _solve_slots(
    slots: list[dict[str, Any]],
    handover_locks: Iterable[dict[str, Any]] = (),
) -> Optional[list[Optional[str]]]:
    """Assign a physical item to every slot.

    A slot is ``{"window": (start, end), "candidates": [...]}``. Two slots may
    use the same item only when their windows do not overlap. ``handover_locks``
    are fixed picked-up intervals: they block the item for any overlapping
    slot. Returns one item id per slot in input order, or ``None``.
    """
    occupied: dict[str, list[tuple[datetime, datetime]]] = {}
    for lock in handover_locks:
        occupied.setdefault(lock["item_id"], []).append(lock["window"])

    if not slots:
        return []

    def free(item_id: str, window: tuple[datetime, datetime]) -> bool:
        for start, end in occupied.get(item_id, ()):
            if _overlaps(start, end, *window):
                return False
        return True

    # Most-constrained slot first; ties keep the caller's (priority) order.
    order = sorted(
        range(len(slots)),
        key=lambda index: (
            sum(
                1
                for item_id in slots[index]["candidates"]
                if free(item_id, slots[index]["window"])
            ),
            index,
        ),
    )
    assignment: list[Optional[str]] = [None] * len(slots)
    failed: set[tuple[int, tuple[tuple[str, str, str], ...]]] = set()

    def backtrack(position: int) -> bool:
        if position == len(order):
            return True
        slot_index = order[position]
        window = slots[slot_index]["window"]
        # Memo key: same prefix position plus exactly which item is occupied in
        # which window. Claiming the same items for different windows is not an
        # equivalent state, so both the item and its interval must be kept.
        state = (
            position,
            tuple(
                sorted(
                    (
                        assignment[index],
                        slots[index]["window"][0].isoformat(),
                        slots[index]["window"][1].isoformat(),
                    )
                    for index in range(len(slots))
                    if assignment[index] is not None
                )
            ),
        )
        if state in failed:
            return False
        for item_id in slots[slot_index]["candidates"]:
            if not free(item_id, window):
                continue
            assignment[slot_index] = item_id
            occupied.setdefault(item_id, []).append(window)
            if backtrack(position + 1):
                return True
            occupied[item_id].pop()
            assignment[slot_index] = None
        failed.add(state)
        return False

    if not backtrack(0):
        return None
    return assignment


def _plan_specs(
    specs: list[dict[str, Any]],
    items: dict[str, dict[str, Any]],
    suspensions: dict[str, list[dict[str, Any]]],
    handover_locks: list[dict[str, Any]],
) -> Optional[dict[str, dict[int, list[str]]]]:
    """Find a complete allocation for every spec, re-planning them jointly.

    ``specs`` are soft (not yet picked up) reservations in priority order. Each
    spec has ``reservation_id``, ``window`` and ``requirements``. The search is
    a single joint backtracking, so it may move any spec's items as long as no
    overlapping windows share one physical item and no handover is disturbed.
    """
    slots: list[dict[str, Any]] = []
    slot_meta: list[tuple[str, int]] = []
    for spec in specs:
        window = spec["window"]
        for requirement in spec["requirements"]:
            tag_set = set(requirement["tags"])
            candidates = [
                item_id
                for item_id, item in items.items()
                if tag_set <= item["tag_set"]
                and not any(
                    _overlaps(window[0], window[1], s["start"], s["end"])
                    for s in suspensions.get(item_id, ())
                )
            ]
            for _ in range(int(requirement["count"])):
                slots.append({"window": window, "candidates": candidates})
                slot_meta.append((spec["reservation_id"], requirement["requirement_id"]))

    assignment = _solve_slots(slots, handover_locks)
    if assignment is None:
        return None

    result: dict[str, dict[int, list[str]]] = {}
    for (reservation_id, requirement_id), item_id in zip(slot_meta, assignment):
        assert item_id is not None
        result.setdefault(reservation_id, {}).setdefault(requirement_id, []).append(item_id)
    return result


# ---------------------------------------------------------------------------
# Shortage diagnostics
# ---------------------------------------------------------------------------


def _diagnose(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    items: dict[str, dict[str, Any]],
    suspensions: dict[str, list[dict[str, Any]]],
    handover_rows: list[sqlite3.Row],
    now: datetime,
    earlier_reservations: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    window = _reservation_window(row, now)
    requirements = _requirements_for(conn, row)
    earlier = list(earlier_reservations)
    handover_by_item: dict[str, list[dict[str, Any]]] = {}
    for handover in handover_rows:
        handover_window = _handover_window(handover, now)
        for allocation in _allocations_for(conn, handover["reservation_id"]):
            if allocation["picked_up_at"] is None or allocation["returned_at"] is not None:
                continue
            handover_by_item.setdefault(allocation["item_id"], []).append(
                {
                    "reservation_id": handover["reservation_id"],
                    "window": handover_window,
                }
            )

    shortfalls: list[dict[str, Any]] = []
    blocked_items: dict[str, set[str]] = {}
    suspended_items: dict[str, set[str]] = {}
    priority_blocked_items: dict[str, set[str]] = {}
    for requirement in requirements:
        tag_set = set(requirement["tags"])
        eligible = [
            item_id for item_id, item in items.items() if tag_set <= item["tag_set"]
        ]
        usable = []
        for item_id in eligible:
            blocked_by_handover = any(
                _overlaps(window[0], window[1], *lock["window"])
                for lock in handover_by_item.get(item_id, ())
            )
            suspended = any(
                _overlaps(window[0], window[1], s["start"], s["end"])
                for s in suspensions.get(item_id, ())
            )
            if blocked_by_handover:
                blocked_items.setdefault(item_id, set()).add(
                    "handover:" + ",".join(
                        sorted(
                            lock["reservation_id"]
                            for lock in handover_by_item[item_id]
                            if _overlaps(window[0], window[1], *lock["window"])
                        )
                    )
                )
            blocked_by_earlier = [
                other["reservation_id"]
                for other in earlier
                if item_id in other["item_ids"]
                and _overlaps(window[0], window[1], *other["window"])
            ]
            if suspended:
                suspended_items.setdefault(item_id, set()).add(
                    ",".join(sorted(requirement["tags"]))
                )
            if blocked_by_earlier:
                priority_blocked_items.setdefault(item_id, set()).add(
                    ",".join(sorted(blocked_by_earlier))
                )
            if not blocked_by_handover and not suspended and not blocked_by_earlier:
                usable.append(item_id)
        if len(usable) < int(requirement["count"]):
            shortfalls.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "tags": requirement["tags"],
                    "required": int(requirement["count"]),
                    "eligible_available": len(usable),
                    "eligible_total": len(eligible),
                }
            )

    # Joint distinct-item pressure: every requirement needs its own items in
    # the same window, so the union of eligible items must cover total demand.
    total_required = sum(int(req["count"]) for req in requirements)
    total_eligible = 0
    if requirements:
        allowed: Optional[set[str]] = None
        for requirement in requirements:
            eligible = {
                item_id
                for item_id, item in items.items()
                if set(requirement["tags"]) <= item["tag_set"]
            }
            allowed = eligible if allowed is None else (allowed | eligible)
        total_eligible = len(allowed or set())

    return {
        "window": {"start_at": window[0].isoformat(), "end_at": window[1].isoformat()},
        "requirement_shortfalls": shortfalls,
        "handover_blocked_items": [
            {"item_id": item_id, "reasons": sorted(reasons)}
            for item_id, reasons in sorted(blocked_items.items())
        ],
        "suspended_items": [
            {"item_id": item_id, "blocked_requirements": sorted(reasons)}
            for item_id, reasons in sorted(suspended_items.items())
        ],
        "blocked_by_earlier_reservations": [
            {"item_id": item_id, "reservation_ids": sorted(reasons)}
            for item_id, reasons in sorted(priority_blocked_items.items())
        ],
        "priority_rule": (
            "earlier accepted reservations keep their complete plan; this one "
            "is shown short and holds no items until capacity returns"
        ),
        "total_required": total_required,
        "total_distinct_eligible": total_eligible,
    }


# ---------------------------------------------------------------------------
# The single evaluation pass
# ---------------------------------------------------------------------------


def _sweep_expired(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Expire accepted bookings whose end time has passed.

    Picked-up handovers are deliberately untouched: an overdue loan keeps its
    items and has no projected release until it is actually returned.
    """
    rows = conn.execute(
        "SELECT * FROM equipment_reservation WHERE status = ? AND end_at <= ? ORDER BY seq",
        (ACCEPTED, now.isoformat()),
    ).fetchall()
    expired_ids: list[str] = []
    for row in rows:
        conn.execute(
            """
            UPDATE equipment_reservation
            SET status = ?, ended_at = ?
            WHERE reservation_id = ?
            """,
            (EXPIRED, now.isoformat(), row["reservation_id"]),
        )
        conn.execute(
            "DELETE FROM equipment_allocation WHERE reservation_id = ? AND picked_up_at IS NULL",
            (row["reservation_id"],),
        )
        expired_ids.append(row["reservation_id"])
    return expired_ids


def _sync_allocations(
    conn: sqlite3.Connection,
    reservation_id: str,
    plan: Optional[dict[int, list[str]]],
    now: datetime,
) -> None:
    """Replace the not-yet-picked allocations with a fresh plan."""
    existing = conn.execute(
        """
        SELECT * FROM equipment_allocation
        WHERE reservation_id = ? AND picked_up_at IS NULL
        ORDER BY allocation_id
        """,
        (reservation_id,),
    ).fetchall()
    desired: set[tuple[int, str]] = set()
    if plan is not None:
        for requirement_id, item_ids in plan.items():
            for item_id in item_ids:
                desired.add((requirement_id, item_id))
    current = {(row["requirement_id"], row["item_id"]) for row in existing}
    if current == desired and existing:
        # Keep stable rows (and their original planned_at) when nothing moved.
        return
    conn.execute(
        "DELETE FROM equipment_allocation WHERE reservation_id = ? AND picked_up_at IS NULL",
        (reservation_id,),
    )
    ts = now.isoformat()
    for requirement_id, item_id in sorted(desired):
        conn.execute(
            """
            INSERT INTO equipment_allocation(
                reservation_id, requirement_id, item_id, planned_at
            ) VALUES (?, ?, ?, ?)
            """,
            (reservation_id, requirement_id, item_id, ts),
        )


def evaluate(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
    focus_reservation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Recompute the reservation picture and refresh planned allocations.

    Must run inside a write transaction. This is the single place that applies
    the priority rule, so every endpoint reports the same doubly-explainable
    state after returns, suspensions, lifts, cancellations and time changes.
    """
    now = now or current_time()
    expired_ids = _sweep_expired(conn, now)
    items = _all_items(conn)
    suspensions = _suspension_intervals(conn)

    rows = conn.execute(
        """
        SELECT * FROM equipment_reservation
        WHERE status IN (?, ?)
        ORDER BY seq
        """,
        (ACCEPTED, PICKED_UP),
    ).fetchall()
    by_id = {row["reservation_id"]: row for row in rows}

    handover_rows = [row for row in rows if row["status"] == PICKED_UP]
    handover_locks: list[dict[str, Any]] = []
    for row in handover_rows:
        window = _handover_window(row, now)
        for allocation in _allocations_for(conn, row["reservation_id"]):
            if allocation["picked_up_at"] is not None and allocation["returned_at"] is None:
                handover_locks.append({"item_id": allocation["item_id"], "window": window})

    soft_specs: list[dict[str, Any]] = []
    for row in rows:
        if row["status"] != ACCEPTED:
            continue
        soft_specs.append(
            {
                "reservation_id": row["reservation_id"],
                "window": _reservation_window(row, now),
                "requirements": _requirements_for(conn, row),
            }
        )

    # Priority rule: keep the first feasible booking, then each later one only
    # if the whole kept set stays jointly feasible. A later booking never
    # displaces an earlier one.
    kept: list[dict[str, Any]] = []
    plans: dict[str, dict[int, list[str]]] = {}
    dropped: dict[str, dict[str, Any]] = {}
    for spec in soft_specs:
        trial = kept + [spec]
        trial_plan = _plan_specs(trial, items, suspensions, handover_locks)
        if trial_plan is None:
            if spec["reservation_id"] == focus_reservation_id:
                earlier = [
                    {
                        "reservation_id": prior["reservation_id"],
                        "window": prior["window"],
                        "item_ids": [
                            item_id
                            for item_ids in plans.get(prior["reservation_id"], {}).values()
                            for item_id in item_ids
                        ],
                    }
                    for prior in kept
                ]
                dropped[spec["reservation_id"]] = _diagnose(
                    conn,
                    by_id[spec["reservation_id"]],
                    items,
                    suspensions,
                    handover_rows,
                    now,
                    earlier,
                )
            continue
        kept = trial
        plans = trial_plan

    for spec in soft_specs:
        reservation_id = spec["reservation_id"]
        _sync_allocations(conn, reservation_id, plans.get(reservation_id), now)

    feasible = {
        spec["reservation_id"]: spec["reservation_id"] in plans for spec in soft_specs
    }
    return {
        "expired_reservation_ids": expired_ids,
        "kept_reservation_ids": [spec["reservation_id"] for spec in kept],
        "plans": plans,
        "feasible": feasible,
        "dropped": dropped,
        "items": items,
        "suspensions": suspensions,
        "handover_rows": handover_rows,
        "now": now,
    }


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _kept_before(
    conn: sqlite3.Connection, row: sqlite3.Row, evaluation: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-state which earlier bookings kept a plan, for shortage explanations."""
    items = evaluation["items"]
    suspensions = evaluation["suspensions"]
    handover_locks: list[dict[str, Any]] = []
    for handover in evaluation.get("handover_rows", []):
        window = _handover_window(handover, evaluation["now"])
        for allocation in _allocations_for(conn, handover["reservation_id"]):
            if allocation["picked_up_at"] is not None and allocation["returned_at"] is None:
                handover_locks.append({"item_id": allocation["item_id"], "window": window})

    kept: list[dict[str, Any]] = []
    plans: dict[str, dict[int, list[str]]] = {}
    earlier_rows = conn.execute(
        """
        SELECT * FROM equipment_reservation
        WHERE status = ? AND seq < ?
        ORDER BY seq
        """,
        (ACCEPTED, row["seq"]),
    ).fetchall()
    for earlier in earlier_rows:
        spec = {
            "reservation_id": earlier["reservation_id"],
            "window": _reservation_window(earlier, evaluation["now"]),
            "requirements": _requirements_for(conn, earlier),
        }
        trial = kept + [spec]
        trial_plan = _plan_specs(trial, items, suspensions, handover_locks)
        if trial_plan is not None:
            kept = trial
            plans = trial_plan
    result = [
        {
            "reservation_id": spec["reservation_id"],
            "window": spec["window"],
            "item_ids": [
                item_id
                for item_ids in plans.get(spec["reservation_id"], {}).values()
                for item_id in item_ids
            ],
        }
        for spec in kept
    ]
    return result, plans


def _reservation_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    now: datetime = evaluation["now"]
    reservation_id = row["reservation_id"]
    status = row["status"]
    requirements = _requirements_for(conn, row)
    allocations = _allocations_for(conn, reservation_id)

    if status == ACCEPTED:
        feasible = evaluation["feasible"].get(reservation_id, False)
        status_label = STATUS_LABELS_ZH[ACCEPTED] if feasible else "缺套"
    else:
        feasible = status in (PICKED_UP, RETURNED)
        status_label = STATUS_LABELS_ZH[status]

    picked_up_at = row["picked_up_at"]
    end = _parse(row["end_at"])
    outstanding = [
        allocation for allocation in allocations
        if allocation["picked_up_at"] is not None and allocation["returned_at"] is None
    ]
    expected_release_at = None
    overdue = False
    if status == PICKED_UP:
        overdue = end < now
        # While an item is actually out we project its release at the agreed
        # end only until that time passes. After that, no projection exists
        # until the physical return is recorded.
        expected_release_at = None if overdue else end.isoformat()

    requirement_payload = []
    allocations_by_requirement: dict[int, list[dict[str, Any]]] = {}
    for allocation in allocations:
        allocations_by_requirement.setdefault(allocation["requirement_id"], []).append(
            {
                "item_id": allocation["item_id"],
                "picked_up_at": allocation["picked_up_at"],
                "returned_at": allocation["returned_at"],
            }
        )
    for requirement in requirements:
        requirement_payload.append(
            {
                "requirement_id": requirement["requirement_id"],
                "tags": requirement["tags"],
                "count": requirement["count"],
                "planned_items": allocations_by_requirement.get(
                    requirement["requirement_id"], []
                ),
            }
        )

    payload = {
        "reservation_id": reservation_id,
        "seq": row["seq"],
        "organizer_school_id": row["organizer_school_id"],
        "term_id": row["term_id"],
        "class_id": row["class_id"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "status": status,
        "status_zh": status_label,
        "accepted": status in (ACCEPTED, PICKED_UP, RETURNED),
        "feasible": feasible,
        "short": status == ACCEPTED and not feasible,
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "picked_up_at": row["picked_up_at"],
        "returned_at": row["returned_at"],
        "cancelled_at": row["cancelled_at"],
        "ended_at": row["ended_at"],
        "requirements": requirement_payload,
        "planned_item_ids": [
            allocation["item_id"]
            for allocation in allocations
            if allocation["picked_up_at"] is None
        ],
        "picked_up_item_ids": [allocation["item_id"] for allocation in outstanding],
        "overdue": overdue,
        "expected_release_at": expected_release_at,
    }
    if status == ACCEPTED and not feasible:
        diagnostics = evaluation["dropped"].get(reservation_id)
        if diagnostics is None:
            kept_earlier, _plans = _kept_before(conn, row, evaluation)
            diagnostics = _diagnose(
                conn,
                row,
                evaluation["items"],
                evaluation["suspensions"],
                evaluation.get("handover_rows", []),
                now,
                kept_earlier,
            )
        payload["shortage"] = diagnostics
    return payload


def _item_payload(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "item_id": item["item_id"],
        "owner_school_id": item["owner_school_id"],
        "name": item["name"],
        "created_at": item["created_at"],
        "tags": item["tags"],
    }
    suspensions = conn.execute(
        """
        SELECT * FROM equipment_suspension
        WHERE item_id = ?
        ORDER BY start_at, suspension_id
        """,
        (item["item_id"],),
    ).fetchall()
    payload["suspensions"] = [
        {
            "suspension_id": row["suspension_id"],
            "item_id": row["item_id"],
            "start_at": row["start_at"],
            "end_at": row["end_at"],
            "reason": row["reason"],
            "lifted_at": row["lifted_at"],
            "active": row["lifted_at"] is None,
        }
        for row in suspensions
    ]
    payload["current_handover"] = None
    handover = conn.execute(
        """
        SELECT a.*, r.organizer_school_id, r.end_at
        FROM equipment_allocation a
        JOIN equipment_reservation r ON r.reservation_id = a.reservation_id
        WHERE a.item_id = ?
          AND a.picked_up_at IS NOT NULL
          AND a.returned_at IS NULL
        """,
        (item["item_id"],),
    ).fetchone()
    if handover is not None:
        end = _parse(handover["end_at"])
        now = current_time()
        payload["current_handover"] = {
            "reservation_id": handover["reservation_id"],
            "organizer_school_id": handover["organizer_school_id"],
            "picked_up_at": handover["picked_up_at"],
            "agreed_end_at": handover["end_at"],
            "overdue": end < now,
            "expected_release_at": None if end < now else end.isoformat(),
        }
    return payload


def _get_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    items = _all_items(conn)
    item = items.get(item_id)
    if item is None:
        raise ServiceError(
            404, "equipment_item_not_found", f"equipment item {item_id} was not found"
        )
    return item


def _get_reservation(conn: sqlite3.Connection, reservation_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM equipment_reservation WHERE reservation_id = ?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        raise ServiceError(
            404,
            "reservation_not_found",
            f"equipment reservation {reservation_id} was not found",
        )
    return row


def _check_organizer(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    organizer_school_id: str,
) -> None:
    if row["organizer_school_id"] != organizer_school_id:
        raise ServiceError(
            403,
            "not_reservation_organizer",
            "only the organizing school that accepted this reservation may change it",
        )


# ---------------------------------------------------------------------------
# Item maintenance (owning school)
# ---------------------------------------------------------------------------


def create_item(conn: sqlite3.Connection, data: EquipmentItemIn) -> dict[str, Any]:
    with write_transaction(conn):
        school = conn.execute(
            "SELECT 1 FROM school WHERE school_id = ?", (data.owner_school_id,)
        ).fetchone()
        if school is None:
            raise ServiceError(
                404,
                "school_not_found",
                f"school {data.owner_school_id} was not found",
            )
        try:
            conn.execute(
                """
                INSERT INTO equipment_item(item_id, owner_school_id, name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (data.item_id, data.owner_school_id, data.name, current_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                409,
                "equipment_item_exists",
                f"equipment item {data.item_id} already exists",
            ) from exc
        for tag in data.tags:
            conn.execute(
                "INSERT INTO equipment_item_tag(item_id, tag) VALUES (?, ?)",
                (data.item_id, tag),
            )
        evaluate(conn)
        item = _get_item(conn, data.item_id)
        return _item_payload(conn, item)


def set_item_tags(
    conn: sqlite3.Connection, item_id: str, data: EquipmentTagsIn
) -> dict[str, Any]:
    with write_transaction(conn):
        item = _get_item(conn, item_id)
        if item["owner_school_id"] != data.owner_school_id:
            raise ServiceError(
                403,
                "not_item_owner",
                "only the owning school may change an item's capability tags",
            )
        conn.execute("DELETE FROM equipment_item_tag WHERE item_id = ?", (item_id,))
        for tag in data.tags:
            conn.execute(
                "INSERT INTO equipment_item_tag(item_id, tag) VALUES (?, ?)",
                (item_id, tag),
            )
        # Any reserved but not yet picked-up plan may have relied on a tag that
        # was just removed; re-evaluate so short sets and restorations surface.
        evaluate(conn)
        return _item_payload(conn, _get_item(conn, item_id))


def list_items(
    conn: sqlite3.Connection,
    owner_school_id: Optional[str] = None,
    tag: Optional[str] = None,
) -> dict[str, Any]:
    with write_transaction(conn):
        evaluate(conn)
        items = _all_items(conn)
        result = []
        for item in items.values():
            if owner_school_id is not None and item["owner_school_id"] != owner_school_id:
                continue
            if tag is not None and tag not in item["tag_set"]:
                continue
            result.append(_item_payload(conn, item))
        return {"items": result}


def get_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    with write_transaction(conn):
        evaluate(conn)
        return _item_payload(conn, _get_item(conn, item_id))


def add_suspension(
    conn: sqlite3.Connection, item_id: str, data: EquipmentSuspensionIn
) -> dict[str, Any]:
    start = _parse(_utc_iso(data.start_at))
    end = _parse(_utc_iso(data.end_at))
    if end <= start:
        raise ServiceError(400, "invalid_suspension_window", "end_at must be after start_at")
    with write_transaction(conn):
        item = _get_item(conn, item_id)
        if item["owner_school_id"] != data.owner_school_id:
            raise ServiceError(
                403,
                "not_item_owner",
                "only the owning school may suspend this item",
            )
        cursor = conn.execute(
            """
            INSERT INTO equipment_suspension(
                item_id, owner_school_id, start_at, end_at, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                data.owner_school_id,
                start.isoformat(),
                end.isoformat(),
                data.reason,
                current_iso(),
            ),
        )
        suspension_id = cursor.lastrowid
        # Suspensions never erase an in-flight handover; evaluate only replans
        # reservations that have not been picked up yet.
        evaluation = evaluate(conn)
        payload = _item_payload(conn, _get_item(conn, item_id))
        payload["created_suspension_id"] = suspension_id
        payload["affected_short_reservation_ids"] = [
            reservation_id
            for reservation_id, feasible in evaluation["feasible"].items()
            if not feasible
        ]
        return payload


def lift_suspension(
    conn: sqlite3.Connection, item_id: str, suspension_id: int, owner_school_id: str
) -> dict[str, Any]:
    with write_transaction(conn):
        item = _get_item(conn, item_id)
        if item["owner_school_id"] != owner_school_id:
            raise ServiceError(
                403,
                "not_item_owner",
                "only the owning school may lift this suspension",
            )
        row = conn.execute(
            "SELECT * FROM equipment_suspension WHERE suspension_id = ? AND item_id = ?",
            (suspension_id, item_id),
        ).fetchone()
        if row is None:
            raise ServiceError(
                404,
                "suspension_not_found",
                f"suspension {suspension_id} was not found for item {item_id}",
            )
        if row["lifted_at"] is None:
            conn.execute(
                "UPDATE equipment_suspension SET lifted_at = ? WHERE suspension_id = ?",
                (current_iso(), suspension_id),
            )
        evaluation = evaluate(conn)
        payload = _item_payload(conn, _get_item(conn, item_id))
        payload["affected_feasible_reservation_ids"] = [
            reservation_id
            for reservation_id, feasible in evaluation["feasible"].items()
            if feasible
        ]
        return payload


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


def create_reservation(
    conn: sqlite3.Connection, data: EquipmentReservationIn
) -> dict[str, Any]:
    start = _parse(_utc_iso(data.start_at))
    end = _parse(_utc_iso(data.end_at))
    if end <= start:
        raise ServiceError(
            400, "invalid_reservation_window", "end_at must be after start_at"
        )
    now = current_time()
    if end <= now:
        raise ServiceError(
            400,
            "reservation_window_in_the_past",
            "a reservation must end after the current time",
        )

    reservation_id = data.reservation_id or f"EQP-{uuid.uuid4().hex[:16].upper()}"
    ts = now.isoformat()

    with write_transaction(conn):
        organizer = conn.execute(
            "SELECT 1 FROM school WHERE school_id = ?",
            (data.organizer_school_id,),
        ).fetchone()
        if organizer is None:
            raise ServiceError(
                404,
                "school_not_found",
                f"school {data.organizer_school_id} was not found",
            )
        term = conn.execute(
            "SELECT * FROM training_term WHERE term_id = ?", (data.term_id,)
        ).fetchone()
        if term is None:
            raise ServiceError(
                404, "term_not_found", f"training term {data.term_id} was not found"
            )
        if term["organizer_school_id"] != data.organizer_school_id:
            raise ServiceError(
                403,
                "not_term_organizer",
                "only the organizing school of this training term may book equipment",
            )
        class_row = conn.execute(
            "SELECT * FROM training_class WHERE class_id = ? AND term_id = ?",
            (data.class_id, data.term_id),
        ).fetchone()
        if class_row is None:
            raise ServiceError(
                404,
                "class_not_found",
                f"class {data.class_id} was not found in term {data.term_id}",
            )
        if conn.execute(
            "SELECT 1 FROM equipment_reservation WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone():
            raise ServiceError(
                409,
                "reservation_exists",
                f"equipment reservation {reservation_id} already exists",
            )

        conn.execute(
            """
            INSERT INTO equipment_reservation(
                reservation_id, organizer_school_id, term_id, class_id,
                start_at, end_at, status, created_at, accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation_id,
                data.organizer_school_id,
                data.term_id,
                data.class_id,
                start.isoformat(),
                end.isoformat(),
                ACCEPTED,
                ts,
                ts,
            ),
        )
        for position, requirement in enumerate(data.requirements):
            cursor = conn.execute(
                """
                INSERT INTO equipment_reservation_requirement(
                    reservation_id, position, count
                ) VALUES (?, ?, ?)
                """,
                (reservation_id, position, requirement.count),
            )
            requirement_id = cursor.lastrowid
            for tag in requirement.tags:
                conn.execute(
                    """
                    INSERT INTO equipment_requirement_tag(requirement_id, tag)
                    VALUES (?, ?)
                    """,
                    (requirement_id, tag),
                )

        # Judge the new booking against the already kept set: ``evaluate`` runs
        # in ``seq`` order, so the freshly inserted last row is only kept if it
        # fits alongside everything accepted before it.
        evaluation = evaluate(conn, now, focus_reservation_id=reservation_id)
        row = _get_reservation(conn, reservation_id)
        payload = _reservation_payload(conn, row, evaluation)
        if row["status"] != ACCEPTED or not payload["feasible"]:
            # A new order must be kept in full; otherwise it is not accepted.
            raise ServiceError(
                409,
                "reservation_not_feasible",
                "this reservation cannot be kept as a complete set right now; "
                "it was not accepted",
                details={
                    "reservation_id": reservation_id,
                    "shortage": payload.get("shortage", {}),
                },
            )
        return payload


def _affected_rows(conn: sqlite3.Connection, organizer_school_id: Optional[str]) -> list[sqlite3.Row]:
    if organizer_school_id is None:
        return conn.execute(
            "SELECT * FROM equipment_reservation ORDER BY seq"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM equipment_reservation WHERE organizer_school_id = ? ORDER BY seq",
        (organizer_school_id,),
    ).fetchall()


def list_reservations(
    conn: sqlite3.Connection,
    organizer_school_id: Optional[str] = None,
    term_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    valid_statuses = (ACCEPTED, PICKED_UP, RETURNED, CANCELLED, EXPIRED)
    if status is not None and status not in valid_statuses:
        raise ServiceError(
            400, "invalid_reservation_status", f"status must be one of {valid_statuses}"
        )
    with write_transaction(conn):
        evaluation = evaluate(conn)
        rows = _affected_rows(conn, organizer_school_id)
        result = []
        for row in rows:
            if term_id is not None and row["term_id"] != term_id:
                continue
            if status is not None and row["status"] != status:
                continue
            result.append(_reservation_payload(conn, row, evaluation))
        return {
            "reservations": result,
            "kept_reservation_ids": evaluation["kept_reservation_ids"],
        }


def get_reservation(
    conn: sqlite3.Connection, reservation_id: str, organizer_school_id: Optional[str] = None
) -> dict[str, Any]:
    with write_transaction(conn):
        evaluation = evaluate(conn, focus_reservation_id=reservation_id)
        row = _get_reservation(conn, reservation_id)
        if organizer_school_id is not None:
            _check_organizer(conn, row, organizer_school_id)
        return _reservation_payload(conn, row, evaluation)


def cancel_reservation(
    conn: sqlite3.Connection, reservation_id: str, organizer_school_id: str
) -> dict[str, Any]:
    with write_transaction(conn):
        evaluation = evaluate(conn)
        row = _get_reservation(conn, reservation_id)
        _check_organizer(conn, row, organizer_school_id)
        if row["status"] != ACCEPTED:
            raise ServiceError(
                409,
                "reservation_not_cancellable",
                "only a reservation that has not been picked up may be cancelled",
                details={"status": row["status"]},
            )
        ts = current_iso()
        conn.execute(
            """
            UPDATE equipment_reservation
            SET status = ?, cancelled_at = ?, ended_at = ?
            WHERE reservation_id = ?
            """,
            (CANCELLED, ts, ts, reservation_id),
        )
        conn.execute(
            "DELETE FROM equipment_allocation WHERE reservation_id = ? AND picked_up_at IS NULL",
            (reservation_id,),
        )
        evaluation = evaluate(conn)
        row = _get_reservation(conn, reservation_id)
        return _reservation_payload(conn, row, evaluation)


def reschedule_reservation(
    conn: sqlite3.Connection,
    reservation_id: str,
    organizer_school_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, Any]:
    start = _parse(_utc_iso(start_at))
    end = _parse(_utc_iso(end_at))
    if end <= start:
        raise ServiceError(
            400, "invalid_reservation_window", "end_at must be after start_at"
        )
    with write_transaction(conn):
        row = _get_reservation(conn, reservation_id)
        _check_organizer(conn, row, organizer_school_id)
        if row["status"] != ACCEPTED:
            raise ServiceError(
                409,
                "reservation_not_reschedulable",
                "only a reservation that has not been picked up may change its time window",
                details={"status": row["status"]},
            )
        if end <= current_time():
            raise ServiceError(
                400,
                "reservation_window_in_the_past",
                "the new window must end after the current time",
            )
        conn.execute(
            "UPDATE equipment_reservation SET start_at = ?, end_at = ? WHERE reservation_id = ?",
            (start.isoformat(), end.isoformat(), reservation_id),
        )
        evaluation = evaluate(conn, focus_reservation_id=reservation_id)
        updated = _get_reservation(conn, reservation_id)
        return _reservation_payload(conn, updated, evaluation)


def _check_pickup_window(row: sqlite3.Row, now: datetime) -> None:
    start = _parse(row["start_at"])
    end = _parse(row["end_at"])
    if now < start:
        raise ServiceError(
            409,
            "reservation_not_started",
            "equipment may only be handed over inside the agreed reservation window",
            details={"start_at": row["start_at"], "now": now.isoformat()},
        )
    if now >= end:
        raise ServiceError(
            409,
            "reservation_window_ended",
            "the agreed reservation window has ended; pick-up is no longer possible",
            details={"end_at": row["end_at"], "now": now.isoformat()},
        )


def pickup(
    conn: sqlite3.Connection, reservation_id: str, data: EquipmentPickupIn
) -> dict[str, Any]:
    with write_transaction(conn):
        now = current_time()
        evaluation = evaluate(conn)
        evaluation_before = evaluation
        row = _get_reservation(conn, reservation_id)
        _check_organizer(conn, row, data.organizer_school_id)
        if row["status"] != ACCEPTED:
            raise ServiceError(
                409,
                "reservation_not_pickable",
                "only an accepted reservation that has not been picked up can be handed over",
                details={"status": row["status"]},
            )
        _check_pickup_window(row, now)
        if not evaluation["feasible"].get(reservation_id, False):
            raise ServiceError(
                409,
                "reservation_short",
                "the reservation cannot currently be kept as a complete set; "
                "pick-up is refused rather than handing over a partial set",
                details={
                    "shortage": _diagnose(
                        conn,
                        row,
                        evaluation["items"],
                        evaluation["suspensions"],
                        [
                            other
                            for other in conn.execute(
                                "SELECT * FROM equipment_reservation WHERE status = ?",
                                (PICKED_UP,),
                            ).fetchall()
                        ],
                        now,
                    )
                },
            )

        items = evaluation["items"]
        requirements = _requirements_for(conn, row)
        requested = list(data.item_ids)
        if len(set(requested)) != len(requested):
            raise ServiceError(
                409,
                "pickup_duplicate_items",
                "one physical item cannot fill two slots in the same handover",
                details={"item_ids": requested},
            )
        total_required = sum(int(requirement["count"]) for requirement in requirements)
        if len(requested) != total_required:
            raise ServiceError(
                409,
                "pickup_item_count_mismatch",
                "the handover must list exactly one physical item per required unit",
                details={
                    "required_item_count": total_required,
                    "listed_item_count": len(requested),
                },
            )

        unknown = [item_id for item_id in requested if item_id not in items]
        if unknown:
            raise ServiceError(
                404,
                "equipment_item_not_found",
                f"unknown equipment item(s): {', '.join(sorted(unknown))}",
                details={"item_ids": sorted(unknown)},
            )

        # No requested item may currently be in somebody else's hands.
        placeholders = ",".join("?" for _ in requested)
        busy_rows = conn.execute(
            f"""
            SELECT a.item_id, a.reservation_id
            FROM equipment_allocation a
            WHERE a.item_id IN ({placeholders})
              AND a.picked_up_at IS NOT NULL
              AND a.returned_at IS NULL
            """,
            requested,
        ).fetchall()
        if busy_rows:
            raise ServiceError(
                409,
                "equipment_item_in_use",
                "one or more physical items are currently held by another reservation",
                details={
                    "busy_items": [
                        {"item_id": busy["item_id"], "reservation_id": busy["reservation_id"]}
                        for busy in busy_rows
                    ]
                },
            )

        window_end = _parse(row["end_at"])
        window = (now, window_end)
        # The physical handover must cover the whole requirement at once, with
        # distinct items, and stay usable for the rest of the promised window.
        slots: list[dict[str, Any]] = []
        for requirement in requirements:
            tag_set = set(requirement["tags"])
            candidates = [
                item_id
                for item_id in requested
                if tag_set <= items[item_id]["tag_set"]
                and not any(
                    _overlaps(window[0], window[1], s["start"], s["end"])
                    for s in evaluation["suspensions"].get(item_id, ())
                )
            ]
            for _ in range(int(requirement["count"])):
                slots.append({"window": window, "candidates": candidates})
        assignment = _solve_slots(slots)
        if assignment is None:
            matching = _diagnose_pickup(requirements, requested, items, evaluation, window)
            raise ServiceError(
                409,
                "pickup_incomplete_set",
                "the listed physical items cannot fill every requirement with a "
                "distinct, currently usable item",
                details={"matching": matching},
            )

        ts = now.isoformat()
        # Replace the reservation's planned rows by the actual handover rows.
        conn.execute(
            "DELETE FROM equipment_allocation WHERE reservation_id = ? AND picked_up_at IS NULL",
            (reservation_id,),
        )
        slot_index = 0
        try:
            for requirement in requirements:
                for _ in range(int(requirement["count"])):
                    item_id = assignment[slot_index]
                    slot_index += 1
                    conn.execute(
                        """
                        INSERT INTO equipment_allocation(
                            reservation_id, requirement_id, item_id, planned_at, picked_up_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (reservation_id, requirement["requirement_id"], item_id, ts, ts),
                    )
            conn.execute(
                """
                UPDATE equipment_reservation
                SET status = ?, picked_up_at = ?
                WHERE reservation_id = ?
                """,
                (PICKED_UP, ts, reservation_id),
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique index uq_equipment_active_handover is the
            # database-level double-lending guard; fail closed as a 409.
            raise ServiceError(
                409,
                "equipment_handover_conflict",
                "a physical item in this handover is already held by another loan",
            ) from exc
        evaluation = evaluate(conn)
        before_feasible = evaluation_before["feasible"]
        displaced = [
            earlier["reservation_id"]
            for earlier in conn.execute(
                "SELECT * FROM equipment_reservation WHERE seq < ? AND status = ? ORDER BY seq",
                (row["seq"], ACCEPTED),
            ).fetchall()
            if before_feasible.get(earlier["reservation_id"], False)
            and not evaluation["feasible"].get(earlier["reservation_id"], False)
        ]
        if displaced:
            # The physical handover would break a reservation accepted earlier.
            # Refuse it and undo the temporary status/allocation writes.
            raise ServiceError(
                409,
                "pickup_displaces_earlier_reservation",
                "this handover would leave an earlier accepted reservation short; "
                "it was refused so priority order stays intact",
                details={"displaced_reservation_ids": displaced},
            )
        updated = _get_reservation(conn, reservation_id)
        return _reservation_payload(conn, updated, evaluation)


def _diagnose_pickup(
    requirements: list[dict[str, Any]],
    requested: list[str],
    items: dict[str, dict[str, Any]],
    evaluation: dict[str, Any],
    window: tuple[datetime, datetime],
) -> dict[str, Any]:
    mismatches = []
    for requirement in requirements:
        tag_set = set(requirement["tags"])
        matching = [
            item_id
            for item_id in requested
            if item_id in items and tag_set <= items[item_id]["tag_set"]
        ]
        suspended = [
            item_id
            for item_id in matching
            if any(
                _overlaps(window[0], window[1], s["start"], s["end"])
                for s in evaluation["suspensions"].get(item_id, ())
            )
        ]
        mismatches.append(
            {
                "requirement_id": requirement["requirement_id"],
                "tags": requirement["tags"],
                "required": requirement["count"],
                "matching_items": matching,
                "suspended_items": suspended,
            }
        )
    return {
        "requested_item_ids": requested,
        "requirements": mismatches,
        "rule": "each requirement needs its own distinct physical item carrying every tag",
    }


def return_items(
    conn: sqlite3.Connection,
    reservation_id: str,
    organizer_school_id: str,
) -> dict[str, Any]:
    with write_transaction(conn):
        now = current_time()
        row = _get_reservation(conn, reservation_id)
        _check_organizer(conn, row, organizer_school_id)
        if row["status"] != PICKED_UP:
            raise ServiceError(
                409,
                "reservation_not_outstanding",
                "only a picked-up reservation with outstanding items can be returned",
                details={"status": row["status"]},
            )
        outstanding = conn.execute(
            """
            SELECT * FROM equipment_allocation
            WHERE reservation_id = ?
              AND picked_up_at IS NOT NULL
              AND returned_at IS NULL
            ORDER BY allocation_id
            """,
            (reservation_id,),
        ).fetchall()
        outstanding_ids = [allocation["item_id"] for allocation in outstanding]
        if not outstanding_ids:
            raise ServiceError(
                409, "nothing_outstanding", "this reservation has no outstanding items"
            )
        # A picked-up set is returned as one whole set; items cannot be
        # swapped or returned piecemeal to keep a booking alive.
        to_return = outstanding_ids
        ts = now.isoformat()
        placeholders = ",".join("?" for _ in to_return)
        conn.execute(
            f"""
            UPDATE equipment_allocation
            SET returned_at = ?
            WHERE reservation_id = ?
              AND item_id IN ({placeholders})
              AND picked_up_at IS NOT NULL
              AND returned_at IS NULL
            """,
            [ts, reservation_id, *to_return],
        )
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS count FROM equipment_allocation
            WHERE reservation_id = ?
              AND picked_up_at IS NOT NULL
              AND returned_at IS NULL
            """,
            (reservation_id,),
        ).fetchone()["count"]
        if remaining == 0:
            conn.execute(
                "UPDATE equipment_reservation SET status = ?, returned_at = ?, ended_at = ? "
                "WHERE reservation_id = ?",
                (RETURNED, ts, ts, reservation_id),
            )
        # Returned items may restore earlier short reservations in priority
        # order; the full evaluation re-plans the remaining soft bookings.
        evaluation = evaluate(conn)
        updated = _get_reservation(conn, reservation_id)
        payload = _reservation_payload(conn, updated, evaluation)
        payload["returned_item_ids"] = to_return
        payload["still_outstanding_item_ids"] = [
            allocation["item_id"]
            for allocation in conn.execute(
                """
                SELECT * FROM equipment_allocation
                WHERE reservation_id = ?
                  AND picked_up_at IS NOT NULL
                  AND returned_at IS NULL
                ORDER BY allocation_id
                """,
                (reservation_id,),
            ).fetchall()
        ]
        payload["restored_feasible_reservation_ids"] = [
            reservation
            for reservation, feasible in evaluation["feasible"].items()
            if feasible
        ]
        return payload


# ---------------------------------------------------------------------------
# Organizer views
# ---------------------------------------------------------------------------


def school_board(conn: sqlite3.Connection, organizer_school_id: str) -> dict[str, Any]:
    """Organizer's board: every reservation of the school and its held numbers."""
    with write_transaction(conn):
        school = conn.execute(
            "SELECT 1 FROM school WHERE school_id = ?", (organizer_school_id,)
        ).fetchone()
        if school is None:
            raise ServiceError(
                404, "school_not_found", f"school {organizer_school_id} was not found"
            )
        evaluation = evaluate(conn)
        rows = conn.execute(
            """
            SELECT * FROM equipment_reservation
            WHERE organizer_school_id = ?
            ORDER BY seq
            """,
            (organizer_school_id,),
        ).fetchall()
        reservations = [_reservation_payload(conn, row, evaluation) for row in rows]
        held = []
        for row in rows:
            if row["status"] != PICKED_UP:
                continue
            for allocation in _allocations_for(conn, row["reservation_id"]):
                if allocation["picked_up_at"] is not None and allocation["returned_at"] is None:
                    held.append(
                        {
                            "item_id": allocation["item_id"],
                            "reservation_id": row["reservation_id"],
                            "class_id": row["class_id"],
                            "term_id": row["term_id"],
                            "picked_up_at": allocation["picked_up_at"],
                        }
                    )
        return {
            "organizer_school_id": organizer_school_id,
            "reservations": reservations,
            "held_items": held,
        }
