"""Training terms, class enrollments and joint swap orders.

Concurrency model
-----------------
Every state-changing operation (and every read that must report consistent
capacity) runs ``BEGIN IMMEDIATE`` first, so all decisions are serialized on
the SQLite write lock. Open swap orders reserve seats immediately on
creation: seats given up by a participant may only be reused inside that same
order, while every class's net increase is taken from capacity that is still
available to nobody else.

Lifecycle
---------
* ``enrollment.status`` is ``enrolled`` while the seat is held, and becomes
  ``withdrawn`` or ``disqualified`` when it is released.
* ``swap_order.status`` stays ``pending_confirmation`` until every participant
  confirms (one atomic ``swapped`` transition), or it ends as ``rejected``,
  ``withdrawn``, ``disqualified`` or ``expired``. Ended orders release their
  reservations and the other students' formal classes never move.
* Expired orders are swept lazily inside each service entry point, so a
  process restart keeps unexpired orders working and never lets an expired
  order block a later seat.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .models import EnrollmentIn, SwapOrderIn, TrainingClassIn, TrainingTermIn
from .services import ServiceError, now_iso, write_transaction

ENROLLED = "enrolled"
WITHDRAWN = "withdrawn"
DISQUALIFIED = "disqualified"

ORDER_PENDING = "pending_confirmation"
ORDER_SWAPPED = "swapped"
ORDER_REJECTED = "rejected"
ORDER_WITHDRAWN = "withdrawn"
ORDER_DISQUALIFIED = "disqualified"
ORDER_EXPIRED = "expired"

ENDED_ORDER_STATUSES = (
    ORDER_SWAPPED,
    ORDER_REJECTED,
    ORDER_WITHDRAWN,
    ORDER_DISQUALIFIED,
    ORDER_EXPIRED,
)

STATUS_LABELS_ZH = {
    ENROLLED: "在册",
    WITHDRAWN: "已退出",
    DISQUALIFIED: "证书失效失去资格",
    ORDER_PENDING: "待确认",
    ORDER_SWAPPED: "已全部确认并完成换班",
    ORDER_REJECTED: "有人拒绝，已结束",
    ORDER_WITHDRAWN: "有人退出报名，已结束",
    ORDER_DISQUALIFIED: "有人失去报名资格，已结束",
    ORDER_EXPIRED: "超过确认截止时间，已结束",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(value: datetime) -> str:
    """Normalize any incoming datetime to a comparable UTC ISO string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _certificate_currently_valid(conn: sqlite3.Connection, certificate_code: str) -> tuple[bool, Optional[sqlite3.Row]]:
    """Validity used for enrollment eligibility.

    A certificate is valid only while its lifecycle is ``active`` and none of
    its (current, non-released) basis records have been revoked at the source.
    Source revocation takes effect immediately; the issuer's later
    confirmation only changes the existing certificate occupancy rules.
    """
    row = conn.execute(
        "SELECT * FROM certificate WHERE certificate_code = ?",
        (certificate_code,),
    ).fetchone()
    if row is None:
        return False, None
    if row["status"] != "active":
        return False, row
    revoked = conn.execute(
        """
        SELECT 1
        FROM certificate_item_record i
        JOIN credit_record r ON r.record_code = i.record_code
        WHERE i.certificate_code = ?
          AND i.released_at IS NULL
          AND r.revoked_at IS NOT NULL
        LIMIT 1
        """,
        (certificate_code,),
    ).fetchone()
    return revoked is None, row


def sweep_expired_orders(conn: sqlite3.Connection) -> list[str]:
    """End every pending order whose deadline has passed.

    Runs inside the caller's write transaction. Ending an order only flips
    flags, so all reservations it held disappear from capacity calculations
    immediately.
    """
    now = now_iso()
    due_rows = conn.execute(
        """
        SELECT order_id FROM swap_order
        WHERE status = ? AND confirm_deadline <= ?
        ORDER BY order_id
        """,
        (ORDER_PENDING, now),
    ).fetchall()
    expired_ids = [row["order_id"] for row in due_rows]
    for order_id in expired_ids:
        _close_order(conn, order_id, ORDER_EXPIRED, "confirm deadline passed")
    return expired_ids


def _close_order(
    conn: sqlite3.Connection,
    order_id: str,
    status: str,
    reason: str,
) -> None:
    ts = now_iso()
    conn.execute(
        """
        UPDATE swap_order
        SET status = ?, completed_at = ?, end_reason = ?
        WHERE order_id = ?
        """,
        (status, ts, reason, order_id),
    )
    conn.execute(
        "UPDATE swap_participant SET order_open = 0 WHERE order_id = ?",
        (order_id,),
    )


def _class_capacity_rows(
    conn: sqlite3.Connection,
    term_id: str,
    exclude_order_id: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Capacity accounting for every class of a term.

    ``enrolled`` counts current formal seats. ``held_pending_in`` counts people
    that open swap orders move into each class. A participant's vacated seat is
    only reusable inside that same order, which is exactly what the per-order
    net check in :func:`_moves_feasible` enforces; across orders and new
    enrollments every held seat is unavailable.

    ``exclude_order_id`` drops one order's own reservations, which is needed
    when finalizing that order: at that point its seats are about to become
    real enrollments, not pending holds.
    """
    classes = conn.execute(
        "SELECT * FROM training_class WHERE term_id = ? ORDER BY class_id",
        (term_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in classes:
        enrolled = conn.execute(
            "SELECT COUNT(*) AS count FROM enrollment WHERE class_id = ? AND status = ?",
            (row["class_id"], ENROLLED),
        ).fetchone()["count"]
        held_in = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM swap_participant p
            JOIN swap_order o ON o.order_id = p.order_id
            WHERE o.term_id = ?
              AND o.status = ?
              AND p.to_class_id = ?
              AND (? IS NULL OR o.order_id <> ?)
            """,
            (term_id, ORDER_PENDING, row["class_id"], exclude_order_id, exclude_order_id),
        ).fetchone()["count"]
        held_out = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM swap_participant p
            JOIN swap_order o ON o.order_id = p.order_id
            WHERE o.term_id = ?
              AND o.status = ?
              AND p.from_class_id = ?
              AND (? IS NULL OR o.order_id <> ?)
            """,
            (term_id, ORDER_PENDING, row["class_id"], exclude_order_id, exclude_order_id),
        ).fetchone()["count"]
        # ``available`` is the seat count open to new enrollments and other
        # orders. Open orders' outgoing seats are reserved for their own
        # incoming moves, so only the net reservation (held_in - held_out)
        # actually consumes externally available capacity; it can never be
        # negative once every order passed the whole-order feasibility check.
        net_held = held_in - held_out
        result[row["class_id"]] = {
            "class_id": row["class_id"],
            "term_id": row["term_id"],
            "name": row["name"],
            "capacity": row["capacity"],
            "enrolled": enrolled,
            "held_pending_in": held_in,
            "held_pending_out": held_out,
            "net_pending_reservation": net_held,
            "available": row["capacity"] - enrolled - net_held,
        }
    return result


def _moves_feasible(
    capacities: dict[str, dict[str, Any]],
    moves: list[tuple[str, str]],
) -> Optional[dict[str, int]]:
    """Check one swap order as a whole.

    For each touched class: seats freed by this order may absorb this order's
    own incoming people, and any net increase must fit in the capacity still
    available after other open orders' reservations.
    """
    out_count: dict[str, int] = {}
    in_count: dict[str, int] = {}
    for from_class_id, to_class_id in moves:
        out_count[from_class_id] = out_count.get(from_class_id, 0) + 1
        in_count[to_class_id] = in_count.get(to_class_id, 0) + 1

    touched = set(out_count) | set(in_count)
    for class_id in touched:
        if class_id not in capacities:
            return None
        info = capacities[class_id]
        outgoing = out_count.get(class_id, 0)
        incoming = in_count.get(class_id, 0)
        net_increase = incoming - outgoing
        # The outgoing seats are reusable by this order, so only the net
        # increase consumes externally available capacity.
        if net_increase > info["available"]:
            return None
    return {class_id: in_count.get(class_id, 0) - out_count.get(class_id, 0) for class_id in touched}


def _get_term_row(conn: sqlite3.Connection, term_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM training_term WHERE term_id = ?", (term_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "term_not_found", f"training term {term_id} was not found")
    return row


def _get_class_row(
    conn: sqlite3.Connection, term_id: str, class_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM training_class WHERE class_id = ? AND term_id = ?",
        (class_id, term_id),
    ).fetchone()
    if row is None:
        raise ServiceError(
            404,
            "class_not_found",
            f"class {class_id} was not found in term {term_id}",
        )
    return row


def _active_enrollment(
    conn: sqlite3.Connection, term_id: str, student_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM enrollment
        WHERE term_id = ? AND student_id = ? AND status = ?
        """,
        (term_id, student_id, ENROLLED),
    ).fetchone()


def _order_detail(conn: sqlite3.Connection, order_row: sqlite3.Row) -> dict[str, Any]:
    participants = conn.execute(
        "SELECT * FROM swap_participant WHERE order_id = ? ORDER BY id",
        (order_row["order_id"],),
    ).fetchall()
    return {
        "order_id": order_row["order_id"],
        "term_id": order_row["term_id"],
        "organizer_school_id": order_row["organizer_school_id"],
        "status": order_row["status"],
        "status_zh": STATUS_LABELS_ZH[order_row["status"]],
        "confirm_deadline": order_row["confirm_deadline"],
        "created_at": order_row["created_at"],
        "completed_at": order_row["completed_at"],
        "end_reason": order_row["end_reason"],
        "participants": [
            {
                "student_id": row["student_id"],
                "from_class_id": row["from_class_id"],
                "to_class_id": row["to_class_id"],
                "response": row["response"],
                "responded_at": row["responded_at"],
            }
            for row in participants
        ],
    }


def _get_open_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "swap_order_not_found", f"swap order {order_id} was not found")
    if row["status"] != ORDER_PENDING:
        raise ServiceError(
            409,
            "swap_order_closed",
            f"swap order {order_id} already ended with status {row['status']}",
            details={"status": row["status"], "end_reason": row["end_reason"]},
        )
    return row


# ---------------------------------------------------------------------------
# Terms and classes
# ---------------------------------------------------------------------------


def create_term(conn: sqlite3.Connection, data: TrainingTermIn) -> dict[str, Any]:
    from .services import get_school  # local import: services imports this module lazily

    get_school(conn, data.organizer_school_id)
    for definition_id in data.accepted_definition_ids:
        definition = conn.execute(
            "SELECT 1 FROM credential_definition WHERE definition_id = ?",
            (definition_id,),
        ).fetchone()
        if definition is None:
            raise ServiceError(
                404,
                "definition_not_found",
                f"credential definition {definition_id} was not found",
            )

    term_id = data.term_id or f"TERM-{uuid.uuid4().hex[:16].upper()}"
    ts = now_iso()
    try:
        with write_transaction(conn):
            conn.execute(
                """
                INSERT INTO training_term(term_id, organizer_school_id, title, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (term_id, data.organizer_school_id, data.title, ts),
            )
            for definition_id in data.accepted_definition_ids:
                conn.execute(
                    """
                    INSERT INTO term_accepted_credential(term_id, definition_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (term_id, definition_id, ts),
                )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409, "term_exists", f"training term {term_id} already exists"
        ) from exc
    return get_term(conn, term_id)


def list_terms(conn: sqlite3.Connection) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        rows = conn.execute(
            "SELECT term_id FROM training_term ORDER BY created_at, term_id"
        ).fetchall()
        terms = [_term_summary(conn, row["term_id"]) for row in rows]
    return {"terms": terms}


def _term_summary(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    term = dict(_get_term_row(conn, term_id))
    accepted_rows = conn.execute(
        "SELECT definition_id FROM term_accepted_credential WHERE term_id = ? ORDER BY definition_id",
        (term_id,),
    ).fetchall()
    term["accepted_definition_ids"] = [row["definition_id"] for row in accepted_rows]
    term["classes"] = list(_class_capacity_rows(conn, term_id).values())
    return term


def get_term(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        summary = _term_summary(conn, term_id)
    return summary


def create_class(conn: sqlite3.Connection, data: TrainingClassIn) -> dict[str, Any]:
    with write_transaction(conn):
        _get_term_row(conn, data.term_id)
        class_id = data.class_id or f"CLASS-{uuid.uuid4().hex[:16].upper()}"
        try:
            conn.execute(
                """
                INSERT INTO training_class(class_id, term_id, name, capacity, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (class_id, data.term_id, data.name, data.capacity, now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                409,
                "class_exists",
                f"class {class_id} already exists or the class name is taken in this term",
            ) from exc
        row = conn.execute(
            "SELECT * FROM training_class WHERE class_id = ?", (class_id,)
        ).fetchone()
        result = _class_capacity_rows(conn, data.term_id)[class_id]
    return {**dict(row), **result}


def list_classes(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    with write_transaction(conn):
        _get_term_row(conn, term_id)
        sweep_expired_orders(conn)
        classes = list(_class_capacity_rows(conn, term_id).values())
    return {"term_id": term_id, "classes": classes}


def get_class_roster(
    conn: sqlite3.Connection, term_id: str, class_id: str, organizer_school_id: str
) -> dict[str, Any]:
    """Organizer view: formal roster plus current capacity and reservations."""
    with write_transaction(conn):
        term = _get_term_row(conn, term_id)
        if term["organizer_school_id"] != organizer_school_id:
            raise ServiceError(
                403,
                "not_term_organizer",
                "only the organizing school may read this term's class roster",
            )
        class_row = _get_class_row(conn, term_id, class_id)
        sweep_expired_orders(conn)
        capacity = _class_capacity_rows(conn, term_id)[class_id]
        enrolled_rows = conn.execute(
            """
            SELECT e.*, c.definition_id, c.issuer_school_id
            FROM enrollment e
            JOIN certificate c ON c.certificate_code = e.certificate_code
            WHERE e.term_id = ? AND e.class_id = ? AND e.status = ?
            ORDER BY e.enrolled_at, e.student_id
            """,
            (term_id, class_id, ENROLLED),
        ).fetchall()
        roster = [
            {
                "student_id": row["student_id"],
                "class_id": row["class_id"],
                "certificate_code": row["certificate_code"],
                "definition_id": row["definition_id"],
                "issuer_school_id": row["issuer_school_id"],
                "enrolled_at": row["enrolled_at"],
            }
            for row in enrolled_rows
        ]
        pending_rows = conn.execute(
            """
            SELECT o.order_id, p.student_id, p.from_class_id, p.to_class_id
            FROM swap_participant p
            JOIN swap_order o ON o.order_id = p.order_id
            WHERE o.term_id = ? AND o.status = ? AND p.to_class_id = ?
            ORDER BY o.order_id, p.id
            """,
            (term_id, ORDER_PENDING, class_id),
        ).fetchall()
        reservations = [dict(row) for row in pending_rows]
    return {
        "term_id": term_id,
        "class": dict(class_row),
        "capacity_view": capacity,
        "roster": roster,
        "pending_incoming_reservations": reservations,
    }


# ---------------------------------------------------------------------------
# Enrollment and withdrawal
# ---------------------------------------------------------------------------


def enroll(conn: sqlite3.Connection, data: EnrollmentIn) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        _get_term_row(conn, data.term_id)
        _get_class_row(conn, data.term_id, data.class_id)
        student = conn.execute(
            "SELECT * FROM student WHERE student_id = ?", (data.student_id,)
        ).fetchone()
        if student is None:
            raise ServiceError(
                404, "student_not_found", f"student {data.student_id} was not found"
            )

        existing = _active_enrollment(conn, data.term_id, data.student_id)
        if existing is not None:
            raise ServiceError(
                409,
                "already_enrolled",
                f"student {data.student_id} already holds a formal seat in class "
                f"{existing['class_id']} of term {data.term_id}; withdraw first",
                details={"current_class_id": existing["class_id"]},
            )

        valid, cert_row = _certificate_currently_valid(conn, data.certificate_code)
        if cert_row is None:
            raise ServiceError(
                404,
                "certificate_not_found",
                f"certificate {data.certificate_code} was not found",
            )
        if not valid:
            raise ServiceError(
                409,
                "certificate_not_valid",
                "enrollment requires one currently valid qualifying certificate",
                details={"certificate_status": cert_row["status"]},
            )
        if cert_row["student_id"] != data.student_id:
            raise ServiceError(
                403,
                "certificate_wrong_student",
                "a student may only enroll with their own certificate",
            )
        accepted = conn.execute(
            """
            SELECT 1 FROM term_accepted_credential
            WHERE term_id = ? AND definition_id = ?
            """,
            (data.term_id, cert_row["definition_id"]),
        ).fetchone()
        if accepted is None:
            raise ServiceError(
                409,
                "credential_type_not_accepted",
                f"term {data.term_id} does not accept credential definition "
                f"{cert_row['definition_id']}",
            )

        capacities = _class_capacity_rows(conn, data.term_id)
        target = capacities[data.class_id]
        if target["available"] <= 0:
            raise ServiceError(
                409,
                "class_full",
                f"class {data.class_id} has no available seat after pending swap reservations",
                details={"capacity_view": target},
            )

        ts = now_iso()
        try:
            cursor = conn.execute(
                """
                INSERT INTO enrollment(
                    term_id, student_id, class_id, certificate_code,
                    definition_id, status, enrolled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.term_id,
                    data.student_id,
                    data.class_id,
                    data.certificate_code,
                    cert_row["definition_id"],
                    ENROLLED,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # uq_active_enrollment: a concurrent transaction won the seat rule.
            raise ServiceError(
                409,
                "enrollment_conflict",
                "enrollment conflicts with a concurrent enrollment or swap",
            ) from exc
        enrollment_id = cursor.lastrowid
        result = _enrollment_payload(conn, enrollment_id)
        result["capacity_view"] = _class_capacity_rows(conn, data.term_id)[data.class_id]
    return result


def _enrollment_payload(conn: sqlite3.Connection, enrollment_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM enrollment WHERE id = ?", (enrollment_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - defensive
        raise ServiceError(404, "enrollment_not_found", "enrollment row disappeared")
    return {
        "enrollment_id": row["id"],
        "term_id": row["term_id"],
        "student_id": row["student_id"],
        "class_id": row["class_id"],
        "certificate_code": row["certificate_code"],
        "definition_id": row["definition_id"],
        "status": row["status"],
        "status_zh": STATUS_LABELS_ZH[row["status"]],
        "enrolled_at": row["enrolled_at"],
        "ended_at": row["ended_at"],
    }


def withdraw(conn: sqlite3.Connection, term_id: str, student_id: str) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        _get_term_row(conn, term_id)
        enrollment_row = _active_enrollment(conn, term_id, student_id)
        if enrollment_row is None:
            raise ServiceError(
                404,
                "active_enrollment_not_found",
                f"student {student_id} has no active enrollment in term {term_id}",
            )
        _end_enrollment_and_orders(
            conn,
            term_id,
            [enrollment_row],
            WITHDRAWN,
            ORDER_WITHDRAWN,
            "a participant withdrew from the term",
        )
        result = _enrollment_payload(conn, enrollment_row["id"])
        result["capacity_view"] = _class_capacity_rows(conn, term_id)[
            enrollment_row["class_id"]
        ]
    return result


def _end_enrollment_and_orders(
    conn: sqlite3.Connection,
    term_id: str,
    enrollment_rows: list[sqlite3.Row],
    enrollment_status: str,
    order_status: str,
    reason: str,
) -> list[str]:
    """Release enrollments and terminate every open order touching them.

    Used for explicit withdrawals and for immediate disqualification when the
    certificate behind an enrollment stops being valid.
    """
    ts = now_iso()
    ended_orders: list[str] = []
    for row in enrollment_rows:
        conn.execute(
            "UPDATE enrollment SET status = ?, ended_at = ? WHERE id = ?",
            (enrollment_status, ts, row["id"]),
        )
        open_orders = conn.execute(
            """
            SELECT DISTINCT o.order_id
            FROM swap_order o
            JOIN swap_participant p ON p.order_id = o.order_id
            WHERE o.term_id = ?
              AND o.status = ?
              AND p.student_id = ?
            """,
            (term_id, ORDER_PENDING, row["student_id"]),
        ).fetchall()
        for order_row in open_orders:
            order_id = order_row["order_id"]
            if order_id in ended_orders:
                continue
            _close_order(conn, order_id, order_status, reason)
            ended_orders.append(order_id)
    return ended_orders


def get_student_term_view(conn: sqlite3.Connection, term_id: str, student_id: str) -> dict[str, Any]:
    """Student view: formal class plus the progress of their swap orders."""
    with write_transaction(conn):
        _get_term_row(conn, term_id)
        sweep_expired_orders(conn)
        student = conn.execute(
            "SELECT 1 FROM student WHERE student_id = ?", (student_id,)
        ).fetchone()
        if student is None:
            raise ServiceError(
                404, "student_not_found", f"student {student_id} was not found"
            )

        enrollment_rows = conn.execute(
            """
            SELECT * FROM enrollment
            WHERE term_id = ? AND student_id = ?
            ORDER BY enrolled_at, id
            """,
            (term_id, student_id),
        ).fetchall()
        enrollments = [_enrollment_payload(conn, row["id"]) for row in enrollment_rows]
        formal = next((item for item in enrollments if item["status"] == ENROLLED), None)

        order_rows = conn.execute(
            """
            SELECT o.*
            FROM swap_order o
            JOIN swap_participant p ON p.order_id = o.order_id
            WHERE o.term_id = ? AND p.student_id = ?
            ORDER BY o.created_at, o.order_id
            """,
            (term_id, student_id),
        ).fetchall()
        orders = [_order_detail(conn, row) for row in order_rows]
        classes = list(_class_capacity_rows(conn, term_id).values())
    return {
        "term_id": term_id,
        "student_id": student_id,
        "formal_enrollment": formal,
        "formal_class_id": formal["class_id"] if formal else None,
        "enrollment_history": enrollments,
        "swap_orders": orders,
        "classes": classes,
    }


# ---------------------------------------------------------------------------
# Swap orders
# ---------------------------------------------------------------------------


def create_swap_order(conn: sqlite3.Connection, data: SwapOrderIn) -> dict[str, Any]:
    deadline = _utc_iso(data.confirm_deadline)
    with write_transaction(conn):
        sweep_expired_orders(conn)
        term = _get_term_row(conn, data.term_id)
        if term["organizer_school_id"] != data.organizer_school_id:
            raise ServiceError(
                403,
                "not_term_organizer",
                "only the organizing school may submit swap orders for this term",
            )
        if deadline <= now_iso():
            raise ServiceError(
                400, "deadline_in_the_past", "confirm_deadline must be in the future"
            )

        # Whole-request validation before any row is written.
        moves: list[tuple[str, str]] = []
        participant_specs: list[dict[str, str]] = []
        for participant in data.participants:
            enrollment_row = _active_enrollment(
                conn, data.term_id, participant.student_id
            )
            if enrollment_row is None:
                raise ServiceError(
                    409,
                    "participant_not_enrolled",
                    f"student {participant.student_id} has no active enrollment in term {data.term_id}",
                )
            from_class_id = enrollment_row["class_id"]
            if from_class_id == participant.to_class_id:
                raise ServiceError(
                    409,
                    "target_same_as_current",
                    f"student {participant.student_id} already belongs to class "
                    f"{from_class_id}; the target class must differ",
                )
            _get_class_row(conn, data.term_id, participant.to_class_id)
            already_open = conn.execute(
                """
                SELECT o.order_id
                FROM swap_participant p
                JOIN swap_order o ON o.order_id = p.order_id
                WHERE p.student_id = ? AND p.order_open = 1 AND o.status = ?
                """,
                (participant.student_id, ORDER_PENDING),
            ).fetchone()
            if already_open is not None:
                raise ServiceError(
                    409,
                    "student_already_in_pending_swap",
                    f"student {participant.student_id} already belongs to open swap order "
                    f"{already_open['order_id']}",
                )
            moves.append((from_class_id, participant.to_class_id))
            participant_specs.append(
                {
                    "student_id": participant.student_id,
                    "from_class_id": from_class_id,
                    "to_class_id": participant.to_class_id,
                }
            )

        capacities = _class_capacity_rows(conn, data.term_id)
        net = _moves_feasible(capacities, moves)
        if net is None:
            touched = sorted({class_id for pair in moves for class_id in pair})
            raise ServiceError(
                409,
                "swap_capacity_exceeded",
                "the swap order as a whole cannot fit: net class increases exceed the "
                "seats currently available after other pending reservations",
                details={
                    "capacity_view": {
                        class_id: capacities[class_id]
                        for class_id in touched
                        if class_id in capacities
                    }
                },
            )

        order_id = data.order_id or f"SWAP-{uuid.uuid4().hex[:16].upper()}"
        ts = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO swap_order(
                    order_id, term_id, organizer_school_id, status,
                    confirm_deadline, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    data.term_id,
                    data.organizer_school_id,
                    ORDER_PENDING,
                    deadline,
                    ts,
                ),
            )
            for spec in participant_specs:
                conn.execute(
                    """
                    INSERT INTO swap_participant(
                        order_id, student_id, from_class_id, to_class_id, order_open
                    ) VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        order_id,
                        spec["student_id"],
                        spec["from_class_id"],
                        spec["to_class_id"],
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                409,
                "swap_order_conflict",
                f"swap order {order_id} conflicts with existing data or a concurrent open order",
            ) from exc
        order_row = conn.execute(
            "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
        ).fetchone()
        result = _order_detail(conn, order_row)
        result["net_class_increases"] = net
        result["capacity_view_after_reservation"] = list(
            _class_capacity_rows(conn, data.term_id).values()
        )
    return result


def respond_swap(
    conn: sqlite3.Connection,
    order_id: str,
    student_id: str,
    response: str,
) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        order_row = _get_open_order(conn, order_id)
        participant_row = conn.execute(
            "SELECT * FROM swap_participant WHERE order_id = ? AND student_id = ?",
            (order_id, student_id),
        ).fetchone()
        if participant_row is None:
            raise ServiceError(
                403,
                "not_swap_participant",
                f"student {student_id} is not a participant of swap order {order_id}",
            )
        if participant_row["response"] is not None:
            raise ServiceError(
                409,
                "already_responded",
                f"student {student_id} already responded "
                f"{participant_row['response']}; late or repeated responses cannot change the order",
                details={"response": participant_row["response"]},
            )

        ts = now_iso()
        if response == "rejected":
            conn.execute(
                "UPDATE swap_participant SET response = ?, responded_at = ? WHERE id = ?",
                ("rejected", ts, participant_row["id"]),
            )
            _close_order(conn, order_id, ORDER_REJECTED, f"student {student_id} rejected the move")
            return _order_detail(
                conn,
                conn.execute(
                    "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
                ).fetchone(),
            )

        # Confirmation: record it first; the order only moves when every
        # participant has confirmed.
        conn.execute(
            "UPDATE swap_participant SET response = ?, responded_at = ? WHERE id = ?",
            ("confirmed", ts, participant_row["id"]),
        )
        pending_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM swap_participant
            WHERE order_id = ? AND response IS NULL
            """,
            (order_id,),
        ).fetchone()["count"]
        if pending_count > 0:
            return _order_detail(
                conn,
                conn.execute(
                    "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
                ).fetchone(),
            )

        # All confirmed: atomic recheck and one-shot class change.
        term_id = order_row["term_id"]
        participants = conn.execute(
            "SELECT * FROM swap_participant WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
        moves = [(row["from_class_id"], row["to_class_id"]) for row in participants]
        # This order's own holds are about to convert into real enrollments,
        # so exclude them when recomputing externally available capacity.
        capacities = _class_capacity_rows(conn, term_id, exclude_order_id=order_id)
        if _moves_feasible(capacities, moves) is None:
            # Reservations were held since creation, so this only happens if
            # stored state was tampered with; fail closed instead of swapping.
            _close_order(
                conn, order_id, ORDER_DISQUALIFIED, "capacity recheck failed at finalization"
            )
            raise ServiceError(
                409,
                "swap_capacity_recheck_failed",
                "the swap order no longer fits and was closed without changing classes",
            )

        for row in participants:
            target_enrollment = _active_enrollment(conn, term_id, row["student_id"])
            if target_enrollment is None:
                _close_order(
                    conn,
                    order_id,
                    ORDER_DISQUALIFIED,
                    f"student {row['student_id']} lost the active enrollment before finalization",
                )
                raise ServiceError(
                    409,
                    "participant_no_longer_enrolled",
                    "a participant is no longer enrolled; the order was closed and nobody moved",
                )
            if target_enrollment["class_id"] != row["from_class_id"]:
                _close_order(
                    conn,
                    order_id,
                    ORDER_DISQUALIFIED,
                    f"student {row['student_id']} changed classes outside this order",
                )
                raise ServiceError(
                    409,
                    "participant_class_changed",
                    "a participant's formal class changed before finalization; order closed",
                )

        # Single atomic move for everyone.
        for row in participants:
            current = _active_enrollment(conn, term_id, row["student_id"])
            conn.execute(
                "UPDATE enrollment SET class_id = ? WHERE id = ?",
                (row["to_class_id"], current["id"]),
            )
        _close_order(conn, order_id, ORDER_SWAPPED, "all participants confirmed")
        final_row = conn.execute(
            "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
        ).fetchone()
        result = _order_detail(conn, final_row)
        result["capacity_view_after_swap"] = list(
            _class_capacity_rows(conn, term_id).values()
        )
    return result


def get_swap_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    with write_transaction(conn):
        sweep_expired_orders(conn)
        row = conn.execute(
            "SELECT * FROM swap_order WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(
                404, "swap_order_not_found", f"swap order {order_id} was not found"
            )
        result = _order_detail(conn, row)
    return result


def list_swap_orders(
    conn: sqlite3.Connection,
    term_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    if status is not None and status not in ENDED_ORDER_STATUSES + (ORDER_PENDING,):
        raise ServiceError(
            400,
            "invalid_swap_status",
            f"status must be one of {(ORDER_PENDING,) + ENDED_ORDER_STATUSES}",
        )
    with write_transaction(conn):
        sweep_expired_orders(conn)
        clauses: list[str] = []
        params: list[Any] = []
        if term_id is not None:
            clauses.append("term_id = ?")
            params.append(term_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM swap_order{where} ORDER BY created_at, order_id",
            params,
        ).fetchall()
        orders = [_order_detail(conn, row) for row in rows]
    return {"orders": orders}


# ---------------------------------------------------------------------------
# Hook invoked by the source-record revocation flow in services.py
# ---------------------------------------------------------------------------


def invalidate_enrollments_for_certificates(
    conn: sqlite3.Connection, certificate_codes: list[str]
) -> dict[str, Any]:
    """Release seats whose certificate stopped being valid.

    Runs inside the revocation write transaction, so the roster, orders and
    remaining seats always explain each other with one serialization order.
    It does not touch certificate occupancy rules for issuance; only training
    enrollments are affected.
    """
    if not certificate_codes:
        return {"disqualified_enrollment_ids": [], "ended_swap_order_ids": []}

    placeholders = ",".join("?" for _ in certificate_codes)
    rows = conn.execute(
        f"""
        SELECT * FROM enrollment
        WHERE status = ? AND certificate_code IN ({placeholders})
        ORDER BY id
        """,
        [ENROLLED, *certificate_codes],
    ).fetchall()
    if not rows:
        return {"disqualified_enrollment_ids": [], "ended_swap_order_ids": []}

    # Group by term because swap orders are term-scoped.
    by_term: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_term.setdefault(row["term_id"], []).append(row)

    ended_orders: list[str] = []
    disqualified_ids: list[int] = []
    for term_id, term_rows in by_term.items():
        disqualified_ids.extend(row["id"] for row in term_rows)
        ended_orders.extend(
            _end_enrollment_and_orders(
                conn,
                term_id,
                term_rows,
                DISQUALIFIED,
                ORDER_DISQUALIFIED,
                "a participant's enrollment certificate stopped being valid",
            )
        )
    return {
        "disqualified_enrollment_ids": disqualified_ids,
        "ended_swap_order_ids": ended_orders,
    }
