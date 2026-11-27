"""Cross-school reconciliation for shared-equipment repair advances.

Domain model
------------
* The organizing school of a training term registers a **share roster**: the
  local schools that split shared-equipment repair costs and each one's
  positive integer share. The roster freezes as soon as the first expense is
  reported for the term.
* The equipment-owning school advances a repair bill and reports one
  **expense** per borrowed physical item per returned borrowing
  (``UNIQUE(reservation_id, item_id)``). The advancing school must itself be
  on the roster and bears its own share.
* Every expense is a chain of **versions**: the original claim plus
  corrections that replace the whole amount (possibly with zero). A version
  takes effect only when the organizer approves it. A correction names the
  approved version it replaces; it must replace the *latest* approved
  version, both when submitted and when approved, so it can never cover a
  later approved version.
* Amounts are integer cents. Each version is split across the roster with
  the largest-remainder method: floors first, leftover cents go to the
  largest fractional remainders, ties broken by ascending school id.
* The organizer closes statement **rounds**. Approved-but-unclosed versions
  enter the round being closed; pending ones wait. Originals book their full
  allocation, corrections book the delta between the new and the replaced
  full allocation. Each round immutably stores its lines plus, per school
  pair, the net owed / arrived / in-flight amounts at close time. Closing an
  already-closed round returns the stored statement; the next round carries
  all previous balances forward.
* Netting is per term and per school pair only: closed allocations and
  arrived money cancel in both directions, never across schools or terms.
  Approved-but-unclosed versions never move payable amounts.
* The paying school registers **transfer orders**; the payee records offline
  **receipts**. A transfer may arrive in parts whose sum cannot exceed its
  face amount. The payee may confirm no more money will arrive, which
  releases the outstanding part while keeping arrived amounts; a closed
  transfer accepts no new receipts.
* A new transfer must follow the pair's current actual debt direction and is
  capped by the **payable amount**: actual debt minus same-direction
  in-flight money. While reverse-direction money may still arrive, no new
  transfer may be issued. Corrections never cancel issued transfers nor stop
  open ones from recording real arrivals; any overpayment simply becomes
  debt in the reverse direction.
* Transfer orders and receipts carry client-supplied resendable ids:
  resending the same id with the same content returns the stored record,
  resending it with different content is rejected.

Concurrency
-----------
Every state change (and every balance read) runs inside ``BEGIN IMMEDIATE``,
so closing, issuing, receipts and transfer closes serialize on the SQLite
write lock and any interleaving is explainable by one serial order. All
history and in-flight balances live in SQLite, so a restart loses nothing.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Optional

from .models import (
    ExpenseCorrectionIn,
    ExpenseIn,
    ReceiptIn,
    ShareRosterIn,
    StatementCloseIn,
    TransferIn,
)
from .services import ServiceError, now_iso, write_transaction

VERSION_PENDING = "pending"
VERSION_APPROVED = "approved"

KIND_ORIGINAL = "original"
KIND_CORRECTION = "correction"

TRANSFER_OPEN = "open"
TRANSFER_CLOSED = "closed"

STATUS_LABELS_ZH = {
    VERSION_PENDING: "待批准",
    VERSION_APPROVED: "已批准",
    KIND_ORIGINAL: "原报费",
    KIND_CORRECTION: "更正",
    TRANSFER_OPEN: "在途",
    TRANSFER_CLOSED: "已关闭",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _get_term(conn: sqlite3.Connection, term_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM training_term WHERE term_id = ?", (term_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "term_not_found", f"training term {term_id} was not found")
    return row


def _require_organizer(term: sqlite3.Row, organizer_school_id: str) -> None:
    if term["organizer_school_id"] != organizer_school_id:
        raise ServiceError(
            403,
            "not_term_organizer",
            "only the term's organizing school may perform this action",
        )


def _roster_rows(conn: sqlite3.Connection, term_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM share_roster WHERE term_id = ? ORDER BY school_id",
        (term_id,),
    ).fetchall()


def _roster_frozen(conn: sqlite3.Connection, term_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM shared_expense WHERE term_id = ? LIMIT 1", (term_id,)
    ).fetchone()
    return row is not None


def _require_roster_member(
    conn: sqlite3.Connection, term_id: str, school_id: str
) -> None:
    row = conn.execute(
        "SELECT 1 FROM share_roster WHERE term_id = ? AND school_id = ?",
        (term_id, school_id),
    ).fetchone()
    if row is None:
        raise ServiceError(
            403,
            "not_in_roster",
            f"school {school_id} is not on the term's share roster",
        )


def _get_school(conn: sqlite3.Connection, school_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM school WHERE school_id = ?", (school_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(
            404, "school_not_found", f"school {school_id} was not found"
        )


def _allocate(amount_cents: int, shares: list[tuple[str, int]]) -> dict[str, int]:
    """Largest-remainder split of ``amount_cents`` across ``shares``.

    Every school first gets the integer floor of its exact share; the
    leftover cents go one each to the largest fractional remainders, ties
    broken by ascending school id.
    """
    total = sum(share for _, share in shares)
    floors: dict[str, int] = {}
    remainders: dict[str, int] = {}
    for school_id, share in shares:
        exact = amount_cents * share
        floors[school_id] = exact // total
        remainders[school_id] = exact % total
    leftover = amount_cents - sum(floors.values())
    ranked = sorted(shares, key=lambda entry: (-remainders[entry[0]], entry[0]))
    result = dict(floors)
    for school_id, _share in ranked[:leftover]:
        result[school_id] += 1
    return result


def _allocation_map(conn: sqlite3.Connection, version_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT school_id, amount_cents FROM expense_allocation WHERE version_id = ?",
        (version_id,),
    ).fetchall()
    return {row["school_id"]: row["amount_cents"] for row in rows}


def _get_expense(conn: sqlite3.Connection, expense_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM shared_expense WHERE expense_id = ?", (expense_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(
            404, "expense_not_found", f"shared expense {expense_id} was not found"
        )
    return row


def _get_transfer(conn: sqlite3.Connection, transfer_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM transfer_order WHERE transfer_id = ?", (transfer_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(
            404, "transfer_not_found", f"transfer order {transfer_id} was not found"
        )
    return row


def _latest_approved_version_no(conn: sqlite3.Connection, expense_id: str) -> int:
    return conn.execute(
        """
        SELECT COALESCE(MAX(version_no), 0) AS latest
        FROM expense_version
        WHERE expense_id = ? AND status = ?
        """,
        (expense_id, VERSION_APPROVED),
    ).fetchone()["latest"]


# ---------------------------------------------------------------------------
# Share roster
# ---------------------------------------------------------------------------


def _roster_payload(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    term = _get_term(conn, term_id)
    rows = _roster_rows(conn, term_id)
    return {
        "term_id": term_id,
        "organizer_school_id": term["organizer_school_id"],
        "frozen": _roster_frozen(conn, term_id),
        "total_shares": sum(row["share"] for row in rows),
        "shares": [
            {
                "school_id": row["school_id"],
                "share": row["share"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }


def set_roster(
    conn: sqlite3.Connection, term_id: str, data: ShareRosterIn
) -> dict[str, Any]:
    """Register (or replace) the whole share roster of a term.

    Allowed until the first expense is reported for the term; afterwards the
    roster is frozen forever.
    """
    with write_transaction(conn):
        term = _get_term(conn, term_id)
        _require_organizer(term, data.organizer_school_id)
        if _roster_frozen(conn, term_id):
            raise ServiceError(
                409,
                "roster_frozen",
                "expense reporting has started for this term; "
                "the share roster can no longer be changed",
            )
        for entry in data.shares:
            _get_school(conn, entry.school_id)
        conn.execute("DELETE FROM share_roster WHERE term_id = ?", (term_id,))
        ts = now_iso()
        for entry in data.shares:
            conn.execute(
                "INSERT INTO share_roster(term_id, school_id, share, created_at) "
                "VALUES (?, ?, ?, ?)",
                (term_id, entry.school_id, entry.share, ts),
            )
        return _roster_payload(conn, term_id)


def get_roster(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    return _roster_payload(conn, term_id)


# ---------------------------------------------------------------------------
# Expenses and versions
# ---------------------------------------------------------------------------


def _insert_version(
    conn: sqlite3.Connection,
    expense: sqlite3.Row,
    version_no: int,
    kind: str,
    amount_cents: int,
    replaces_version: Optional[int],
    submitted_by: str,
) -> int:
    """Insert one pending version together with its full roster allocation."""
    ts = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO expense_version(
            expense_id, version_no, kind, amount_cents, replaces_version,
            status, submitted_by, submitted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            expense["expense_id"],
            version_no,
            kind,
            amount_cents,
            replaces_version,
            VERSION_PENDING,
            submitted_by,
            ts,
        ),
    )
    version_id = int(cursor.lastrowid)
    shares = [
        (row["school_id"], row["share"])
        for row in _roster_rows(conn, expense["term_id"])
    ]
    for school_id, cents in _allocate(amount_cents, shares).items():
        conn.execute(
            "INSERT INTO expense_allocation(version_id, school_id, amount_cents) "
            "VALUES (?, ?, ?)",
            (version_id, school_id, cents),
        )
    return version_id


def create_expense(
    conn: sqlite3.Connection, term_id: str, data: ExpenseIn
) -> dict[str, Any]:
    """Report one advanced expense for a returned borrowed item."""
    with write_transaction(conn):
        _get_term(conn, term_id)
        # The advancing school must share in the costs it advances.
        _require_roster_member(conn, term_id, data.advancing_school_id)
        reservation = conn.execute(
            "SELECT * FROM equipment_reservation WHERE reservation_id = ?",
            (data.reservation_id,),
        ).fetchone()
        if reservation is None:
            raise ServiceError(
                404,
                "reservation_not_found",
                f"equipment reservation {data.reservation_id} was not found",
            )
        if reservation["term_id"] != term_id:
            raise ServiceError(
                409,
                "reservation_term_mismatch",
                "the reservation does not belong to this training term",
                details={"reservation_term_id": reservation["term_id"]},
            )
        item = conn.execute(
            "SELECT * FROM equipment_item WHERE item_id = ?", (data.item_id,)
        ).fetchone()
        if item is None:
            raise ServiceError(
                404,
                "equipment_item_not_found",
                f"equipment item {data.item_id} was not found",
            )
        if item["owner_school_id"] != data.advancing_school_id:
            raise ServiceError(
                403,
                "not_item_owner",
                "only the school that owns the equipment item may advance its costs",
            )
        allocation = conn.execute(
            """
            SELECT * FROM equipment_allocation
            WHERE reservation_id = ? AND item_id = ?
            """,
            (data.reservation_id, data.item_id),
        ).fetchone()
        if allocation is None or allocation["picked_up_at"] is None:
            raise ServiceError(
                409,
                "item_not_borrowed",
                "this item was not handed out under that reservation",
            )
        if allocation["returned_at"] is None:
            raise ServiceError(
                409,
                "borrow_not_returned",
                "only actually returned borrowings can be expensed",
            )
        duplicate = conn.execute(
            "SELECT expense_id FROM shared_expense WHERE reservation_id = ? AND item_id = ?",
            (data.reservation_id, data.item_id),
        ).fetchone()
        if duplicate is not None:
            raise ServiceError(
                409,
                "expense_exists",
                "this borrowing of the item already has an expense",
                details={"expense_id": duplicate["expense_id"]},
            )
        expense_id = data.expense_id or f"EXP-{uuid.uuid4().hex[:16].upper()}"
        ts = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO shared_expense(
                    expense_id, term_id, reservation_id, item_id,
                    advancing_school_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    expense_id,
                    term_id,
                    data.reservation_id,
                    data.item_id,
                    data.advancing_school_id,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                409, "expense_id_exists", f"expense id {expense_id} already exists"
            ) from exc
        expense = _get_expense(conn, expense_id)
        _insert_version(
            conn,
            expense,
            version_no=1,
            kind=KIND_ORIGINAL,
            amount_cents=data.amount_cents,
            replaces_version=None,
            submitted_by=data.advancing_school_id,
        )
        return _expense_payload(conn, expense_id)


def create_correction(
    conn: sqlite3.Connection, expense_id: str, data: ExpenseCorrectionIn
) -> dict[str, Any]:
    """Submit a correction replacing the latest approved version's amount."""
    with write_transaction(conn):
        expense = _get_expense(conn, expense_id)
        if expense["advancing_school_id"] != data.advancing_school_id:
            raise ServiceError(
                403,
                "not_advancing_school",
                "only the advancing school may correct its own expense",
            )
        base = conn.execute(
            "SELECT * FROM expense_version WHERE expense_id = ? AND version_no = ?",
            (expense_id, data.replaces_version),
        ).fetchone()
        if base is None:
            raise ServiceError(
                404,
                "version_not_found",
                f"expense {expense_id} has no version {data.replaces_version}",
            )
        if base["status"] != VERSION_APPROVED:
            raise ServiceError(
                409,
                "base_version_not_approved",
                "a correction may only replace an approved version",
            )
        latest = _latest_approved_version_no(conn, expense_id)
        if data.replaces_version != latest:
            raise ServiceError(
                409,
                "stale_base_version",
                "a correction must replace the latest approved version; "
                "it cannot cover later approved versions",
                details={"latest_approved_version_no": latest},
            )
        next_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
            "FROM expense_version WHERE expense_id = ?",
            (expense_id,),
        ).fetchone()["next_no"]
        _insert_version(
            conn,
            expense,
            version_no=next_no,
            kind=KIND_CORRECTION,
            amount_cents=data.new_amount_cents,
            replaces_version=data.replaces_version,
            submitted_by=data.advancing_school_id,
        )
        return _expense_payload(conn, expense_id)


def approve_version(
    conn: sqlite3.Connection,
    expense_id: str,
    version_no: int,
    organizer_school_id: str,
) -> dict[str, Any]:
    """Organizer approval; only approved versions may enter a statement."""
    with write_transaction(conn):
        expense = _get_expense(conn, expense_id)
        term = _get_term(conn, expense["term_id"])
        _require_organizer(term, organizer_school_id)
        version = conn.execute(
            "SELECT * FROM expense_version WHERE expense_id = ? AND version_no = ?",
            (expense_id, version_no),
        ).fetchone()
        if version is None:
            raise ServiceError(
                404,
                "version_not_found",
                f"expense {expense_id} has no version {version_no}",
            )
        if version["status"] != VERSION_PENDING:
            raise ServiceError(
                409,
                "version_already_approved",
                f"version {version_no} of expense {expense_id} is already approved",
            )
        if version["kind"] == KIND_CORRECTION:
            latest = _latest_approved_version_no(conn, expense_id)
            if version["replaces_version"] != latest:
                raise ServiceError(
                    409,
                    "stale_base_version",
                    "the version this correction replaces is no longer the latest "
                    "approved version; approving it would cover a later approval",
                    details={"latest_approved_version_no": latest},
                )
        conn.execute(
            """
            UPDATE expense_version
            SET status = ?, approved_by = ?, approved_at = ?
            WHERE version_id = ?
            """,
            (VERSION_APPROVED, organizer_school_id, now_iso(), version["version_id"]),
        )
        return _expense_payload(conn, expense_id)


def _version_payload(conn: sqlite3.Connection, version: sqlite3.Row) -> dict[str, Any]:
    allocations = conn.execute(
        """
        SELECT school_id, amount_cents FROM expense_allocation
        WHERE version_id = ? ORDER BY school_id
        """,
        (version["version_id"],),
    ).fetchall()
    return {
        "version_no": version["version_no"],
        "kind": version["kind"],
        "kind_label": STATUS_LABELS_ZH[version["kind"]],
        "amount_cents": version["amount_cents"],
        "replaces_version": version["replaces_version"],
        "status": version["status"],
        "status_label": STATUS_LABELS_ZH[version["status"]],
        "submitted_by": version["submitted_by"],
        "submitted_at": version["submitted_at"],
        "approved_by": version["approved_by"],
        "approved_at": version["approved_at"],
        "round_no": version["round_no"],
        "allocations": [
            {"school_id": row["school_id"], "amount_cents": row["amount_cents"]}
            for row in allocations
        ],
    }


def _expense_payload(conn: sqlite3.Connection, expense_id: str) -> dict[str, Any]:
    expense = _get_expense(conn, expense_id)
    versions = conn.execute(
        "SELECT * FROM expense_version WHERE expense_id = ? ORDER BY version_no",
        (expense_id,),
    ).fetchall()
    latest_approved = _latest_approved_version_no(conn, expense_id)
    approved_amount: Optional[int] = None
    if latest_approved:
        approved_amount = conn.execute(
            "SELECT amount_cents FROM expense_version "
            "WHERE expense_id = ? AND version_no = ?",
            (expense_id, latest_approved),
        ).fetchone()["amount_cents"]
    return {
        "expense_id": expense["expense_id"],
        "term_id": expense["term_id"],
        "reservation_id": expense["reservation_id"],
        "item_id": expense["item_id"],
        "advancing_school_id": expense["advancing_school_id"],
        "created_at": expense["created_at"],
        "current_approved_version_no": latest_approved or None,
        "current_approved_amount_cents": approved_amount,
        "versions": [_version_payload(conn, version) for version in versions],
    }


def get_expense(conn: sqlite3.Connection, expense_id: str) -> dict[str, Any]:
    return _expense_payload(conn, expense_id)


def list_expenses(
    conn: sqlite3.Connection,
    term_id: str,
    advancing_school_id: Optional[str] = None,
) -> dict[str, Any]:
    _get_term(conn, term_id)
    query = "SELECT expense_id FROM shared_expense WHERE term_id = ?"
    params: list[Any] = [term_id]
    if advancing_school_id is not None:
        query += " AND advancing_school_id = ?"
        params.append(advancing_school_id)
    query += " ORDER BY created_at, expense_id"
    rows = conn.execute(query, params).fetchall()
    return {
        "term_id": term_id,
        "expenses": [_expense_payload(conn, row["expense_id"]) for row in rows],
    }


# ---------------------------------------------------------------------------
# Pair balances: charges, arrived money and in-flight money per school pair
# ---------------------------------------------------------------------------


def _pair_components(
    conn: sqlite3.Connection, term_id: str
) -> tuple[
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
    dict[tuple[str, str], int],
]:
    """Directed per-pair totals for one term.

    ``charges[(debtor, creditor)]`` sums booked (closed) allocations the
    debtor owes the creditor; the advancing school's own share is self-borne
    and never becomes a charge. ``received[(payer, payee)]`` sums arrived
    receipts. ``inflight[(payer, payee)]`` sums the still-outstanding part of
    open transfers — money that may still arrive.
    """
    charges: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        """
        SELECT la.school_id AS debtor, e.advancing_school_id AS creditor,
               SUM(la.delta_cents) AS total
        FROM statement_line_allocation la
        JOIN shared_expense e ON e.expense_id = la.expense_id
        WHERE la.term_id = ? AND la.school_id <> e.advancing_school_id
        GROUP BY la.school_id, e.advancing_school_id
        """,
        (term_id,),
    ).fetchall()
    for row in rows:
        if row["total"]:
            charges[(row["debtor"], row["creditor"])] = row["total"]

    received: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        """
        SELECT t.payer_school_id AS payer, t.payee_school_id AS payee,
               SUM(r.amount_cents) AS total
        FROM transfer_order t
        JOIN transfer_receipt r ON r.transfer_id = t.transfer_id
        WHERE t.term_id = ?
        GROUP BY t.payer_school_id, t.payee_school_id
        """,
        (term_id,),
    ).fetchall()
    for row in rows:
        received[(row["payer"], row["payee"])] = row["total"]

    inflight: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        """
        SELECT t.transfer_id, t.payer_school_id AS payer, t.payee_school_id AS payee,
               t.amount_cents - COALESCE(SUM(r.amount_cents), 0) AS outstanding
        FROM transfer_order t
        LEFT JOIN transfer_receipt r ON r.transfer_id = t.transfer_id
        WHERE t.term_id = ? AND t.status = ?
        GROUP BY t.transfer_id
        """,
        (term_id, TRANSFER_OPEN),
    ).fetchall()
    for row in rows:
        if row["outstanding"] > 0:
            key = (row["payer"], row["payee"])
            inflight[key] = inflight.get(key, 0) + row["outstanding"]
    return charges, received, inflight


def _pair_state(
    charges: dict[tuple[str, str], int],
    received: dict[tuple[str, str], int],
    inflight: dict[tuple[str, str], int],
    school_x: str,
    school_y: str,
) -> dict[str, int]:
    """Net position of the ordered pair (x, y); ``net_xy`` > 0 means x owes y."""
    charges_xy = charges.get((school_x, school_y), 0)
    charges_yx = charges.get((school_y, school_x), 0)
    received_xy = received.get((school_x, school_y), 0)
    received_yx = received.get((school_y, school_x), 0)
    return {
        "charges_xy": charges_xy,
        "charges_yx": charges_yx,
        "received_xy": received_xy,
        "received_yx": received_yx,
        "inflight_xy": inflight.get((school_x, school_y), 0),
        "inflight_yx": inflight.get((school_y, school_x), 0),
        # Closed allocations and arrived money net in both directions.
        "net_xy": charges_xy - received_xy - charges_yx + received_yx,
    }


def _debt_view(net_xy: int, school_x: str, school_y: str) -> Optional[dict[str, Any]]:
    if net_xy > 0:
        return {
            "debtor_school_id": school_x,
            "creditor_school_id": school_y,
            "amount_cents": net_xy,
        }
    if net_xy < 0:
        return {
            "debtor_school_id": school_y,
            "creditor_school_id": school_x,
            "amount_cents": -net_xy,
        }
    return None


def _payable_from_state(state: dict[str, int]) -> dict[str, Any]:
    """Payable view for the direction x -> y of a pair state."""
    net = state["net_xy"]
    blocked = state["inflight_yx"] > 0
    if net <= 0:
        payable = 0
    elif blocked:
        payable = 0
    else:
        payable = max(0, net - state["inflight_xy"])
    return {
        "actual_debt_cents": max(net, 0),
        "inflight_forward_cents": state["inflight_xy"],
        "inflight_reverse_cents": state["inflight_yx"],
        "blocked_by_reverse_inflight": blocked,
        "payable_cents": payable,
    }


# ---------------------------------------------------------------------------
# Statement rounds
# ---------------------------------------------------------------------------


def close_round(
    conn: sqlite3.Connection, term_id: str, data: StatementCloseIn
) -> tuple[dict[str, Any], bool]:
    """Close one statement round; returns (payload, created).

    Closing an already-closed round returns the stored statement unchanged.
    """
    with write_transaction(conn):
        term = _get_term(conn, term_id)
        _require_organizer(term, data.organizer_school_id)
        existing = conn.execute(
            "SELECT 1 FROM statement WHERE term_id = ? AND round_no = ?",
            (term_id, data.round_no),
        ).fetchone()
        if existing is not None:
            return _statement_payload(conn, term_id, data.round_no), False
        max_round = conn.execute(
            "SELECT COALESCE(MAX(round_no), 0) AS max_round FROM statement "
            "WHERE term_id = ?",
            (term_id,),
        ).fetchone()["max_round"]
        if data.round_no != max_round + 1:
            raise ServiceError(
                409,
                "round_out_of_order",
                f"the next round that can be closed is {max_round + 1}",
                details={"next_round_no": max_round + 1},
            )
        # Approved-but-unclosed versions enter this round; pending ones wait.
        versions = conn.execute(
            """
            SELECT v.* FROM expense_version v
            JOIN shared_expense e ON e.expense_id = v.expense_id
            WHERE e.term_id = ? AND v.status = ? AND v.round_no IS NULL
            ORDER BY v.version_id
            """,
            (term_id, VERSION_APPROVED),
        ).fetchall()
        conn.execute(
            "INSERT INTO statement(term_id, round_no, closed_by, closed_at) "
            "VALUES (?, ?, ?, ?)",
            (term_id, data.round_no, data.organizer_school_id, now_iso()),
        )
        for version in versions:
            conn.execute(
                """
                INSERT INTO statement_line(term_id, round_no, expense_id, version_no, kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    term_id,
                    data.round_no,
                    version["expense_id"],
                    version["version_no"],
                    version["kind"],
                ),
            )
            new_alloc = _allocation_map(conn, version["version_id"])
            if version["kind"] == KIND_CORRECTION:
                base = conn.execute(
                    "SELECT version_id FROM expense_version "
                    "WHERE expense_id = ? AND version_no = ?",
                    (version["expense_id"], version["replaces_version"]),
                ).fetchone()
                base_alloc = _allocation_map(conn, base["version_id"])
            else:
                base_alloc = {}
            # Originals book their full allocation; corrections book the delta
            # between the new and the replaced full allocation.
            for school_id in sorted(set(new_alloc) | set(base_alloc)):
                delta = new_alloc.get(school_id, 0) - base_alloc.get(school_id, 0)
                conn.execute(
                    """
                    INSERT INTO statement_line_allocation(
                        term_id, round_no, expense_id, version_no, school_id, delta_cents
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        term_id,
                        data.round_no,
                        version["expense_id"],
                        version["version_no"],
                        school_id,
                        delta,
                    ),
                )
            conn.execute(
                "UPDATE expense_version SET round_no = ? WHERE version_id = ?",
                (data.round_no, version["version_id"]),
            )
        # Snapshot every school pair that has any money movement at close time.
        charges, received, inflight = _pair_components(conn, term_id)
        pairs: set[tuple[str, str]] = set()
        for mapping in (charges, received, inflight):
            for school_x, school_y in mapping:
                pairs.add((min(school_x, school_y), max(school_x, school_y)))
        for school_a, school_b in sorted(pairs):
            state = _pair_state(charges, received, inflight, school_a, school_b)
            conn.execute(
                """
                INSERT INTO statement_pair(
                    term_id, round_no, school_a, school_b,
                    charges_a_to_b, charges_b_to_a,
                    received_a_to_b, received_b_to_a,
                    inflight_a_to_b, inflight_b_to_a, net_a_to_b
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    term_id,
                    data.round_no,
                    school_a,
                    school_b,
                    state["charges_xy"],
                    state["charges_yx"],
                    state["received_xy"],
                    state["received_yx"],
                    state["inflight_xy"],
                    state["inflight_yx"],
                    state["net_xy"],
                ),
            )
        return _statement_payload(conn, term_id, data.round_no), True


def _statement_payload(
    conn: sqlite3.Connection, term_id: str, round_no: int
) -> dict[str, Any]:
    statement = conn.execute(
        "SELECT * FROM statement WHERE term_id = ? AND round_no = ?",
        (term_id, round_no),
    ).fetchone()
    if statement is None:
        raise ServiceError(
            404,
            "statement_not_found",
            f"term {term_id} has no closed statement round {round_no}",
        )
    lines = conn.execute(
        "SELECT * FROM statement_line WHERE term_id = ? AND round_no = ? "
        "ORDER BY expense_id, version_no",
        (term_id, round_no),
    ).fetchall()
    line_payloads = []
    for line in lines:
        expense = _get_expense(conn, line["expense_id"])
        version = conn.execute(
            "SELECT * FROM expense_version WHERE expense_id = ? AND version_no = ?",
            (line["expense_id"], line["version_no"]),
        ).fetchone()
        allocations = conn.execute(
            """
            SELECT school_id, delta_cents FROM statement_line_allocation
            WHERE term_id = ? AND round_no = ? AND expense_id = ? AND version_no = ?
            ORDER BY school_id
            """,
            (term_id, round_no, line["expense_id"], line["version_no"]),
        ).fetchall()
        line_payloads.append(
            {
                "expense_id": line["expense_id"],
                "version_no": line["version_no"],
                "kind": line["kind"],
                "kind_label": STATUS_LABELS_ZH[line["kind"]],
                "reservation_id": expense["reservation_id"],
                "item_id": expense["item_id"],
                "advancing_school_id": expense["advancing_school_id"],
                "amount_cents": version["amount_cents"],
                "replaces_version": version["replaces_version"],
                "allocations": [
                    {"school_id": row["school_id"], "delta_cents": row["delta_cents"]}
                    for row in allocations
                ],
            }
        )
    pairs = conn.execute(
        "SELECT * FROM statement_pair WHERE term_id = ? AND round_no = ? "
        "ORDER BY school_a, school_b",
        (term_id, round_no),
    ).fetchall()
    pair_payloads = []
    for pair in pairs:
        pair_payloads.append(
            {
                "school_a": pair["school_a"],
                "school_b": pair["school_b"],
                "charges_a_to_b_cents": pair["charges_a_to_b"],
                "charges_b_to_a_cents": pair["charges_b_to_a"],
                "received_a_to_b_cents": pair["received_a_to_b"],
                "received_b_to_a_cents": pair["received_b_to_a"],
                "inflight_a_to_b_cents": pair["inflight_a_to_b"],
                "inflight_b_to_a_cents": pair["inflight_b_to_a"],
                "net_owed": _debt_view(
                    pair["net_a_to_b"], pair["school_a"], pair["school_b"]
                ),
            }
        )
    return {
        "term_id": term_id,
        "round_no": round_no,
        "closed_by": statement["closed_by"],
        "closed_at": statement["closed_at"],
        "lines": line_payloads,
        "pairs": pair_payloads,
    }


def get_statement(
    conn: sqlite3.Connection, term_id: str, round_no: int
) -> dict[str, Any]:
    return _statement_payload(conn, term_id, round_no)


def list_statements(conn: sqlite3.Connection, term_id: str) -> dict[str, Any]:
    _get_term(conn, term_id)
    rows = conn.execute(
        "SELECT * FROM statement WHERE term_id = ? ORDER BY round_no", (term_id,)
    ).fetchall()
    summaries = []
    for row in rows:
        line_count = conn.execute(
            "SELECT COUNT(*) AS count FROM statement_line "
            "WHERE term_id = ? AND round_no = ?",
            (term_id, row["round_no"]),
        ).fetchone()["count"]
        summaries.append(
            {
                "term_id": term_id,
                "round_no": row["round_no"],
                "closed_by": row["closed_by"],
                "closed_at": row["closed_at"],
                "line_count": line_count,
            }
        )
    return {"term_id": term_id, "statements": summaries}


# ---------------------------------------------------------------------------
# Transfers and receipts
# ---------------------------------------------------------------------------


def create_transfer(
    conn: sqlite3.Connection, term_id: str, data: TransferIn
) -> tuple[dict[str, Any], bool]:
    """Register a transfer order; returns (payload, created).

    Resending an existing transfer id with the same content returns the
    stored order; the same id with different content is rejected.
    """
    with write_transaction(conn):
        _get_term(conn, term_id)
        existing = conn.execute(
            "SELECT * FROM transfer_order WHERE transfer_id = ?", (data.transfer_id,)
        ).fetchone()
        if existing is not None:
            same_content = (
                existing["term_id"] == term_id
                and existing["payer_school_id"] == data.payer_school_id
                and existing["payee_school_id"] == data.payee_school_id
                and existing["amount_cents"] == data.amount_cents
            )
            if same_content:
                return _transfer_payload(conn, existing["transfer_id"]), False
            raise ServiceError(
                409,
                "transfer_id_conflict",
                f"transfer id {data.transfer_id} already exists with different content",
            )
        _require_roster_member(conn, term_id, data.payer_school_id)
        _require_roster_member(conn, term_id, data.payee_school_id)
        charges, received, inflight = _pair_components(conn, term_id)
        state = _pair_state(
            charges, received, inflight, data.payer_school_id, data.payee_school_id
        )
        view = _payable_from_state(state)
        if state["net_xy"] <= 0:
            raise ServiceError(
                409,
                "no_payable_debt",
                "the pair currently has no actual debt in this direction",
                details={"payable_cents": 0},
            )
        if view["blocked_by_reverse_inflight"]:
            raise ServiceError(
                409,
                "reverse_inflight",
                "reverse-direction transfers may still arrive; "
                "no new transfer can be issued now",
                details={"inflight_reverse_cents": state["inflight_yx"]},
            )
        if data.amount_cents > view["payable_cents"]:
            raise ServiceError(
                409,
                "amount_exceeds_payable",
                "the amount exceeds the pair's current payable amount",
                details={"payable_cents": view["payable_cents"]},
            )
        conn.execute(
            """
            INSERT INTO transfer_order(
                transfer_id, term_id, payer_school_id, payee_school_id,
                amount_cents, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.transfer_id,
                term_id,
                data.payer_school_id,
                data.payee_school_id,
                data.amount_cents,
                TRANSFER_OPEN,
                now_iso(),
            ),
        )
        return _transfer_payload(conn, data.transfer_id), True


def _transfer_payload(conn: sqlite3.Connection, transfer_id: str) -> dict[str, Any]:
    transfer = _get_transfer(conn, transfer_id)
    receipts = conn.execute(
        "SELECT * FROM transfer_receipt WHERE transfer_id = ? ORDER BY received_at, receipt_id",
        (transfer_id,),
    ).fetchall()
    received_total = sum(receipt["amount_cents"] for receipt in receipts)
    return {
        "transfer_id": transfer["transfer_id"],
        "term_id": transfer["term_id"],
        "payer_school_id": transfer["payer_school_id"],
        "payee_school_id": transfer["payee_school_id"],
        "amount_cents": transfer["amount_cents"],
        "received_cents": received_total,
        "outstanding_cents": transfer["amount_cents"] - received_total,
        "status": transfer["status"],
        "status_label": STATUS_LABELS_ZH[transfer["status"]],
        "created_at": transfer["created_at"],
        "closed_at": transfer["closed_at"],
        "receipts": [
            {
                "receipt_id": receipt["receipt_id"],
                "amount_cents": receipt["amount_cents"],
                "recorded_by": receipt["recorded_by"],
                "received_at": receipt["received_at"],
            }
            for receipt in receipts
        ],
    }


def get_transfer(conn: sqlite3.Connection, transfer_id: str) -> dict[str, Any]:
    return _transfer_payload(conn, transfer_id)


def list_transfers(
    conn: sqlite3.Connection, term_id: str, school_id: Optional[str] = None
) -> dict[str, Any]:
    _get_term(conn, term_id)
    query = "SELECT transfer_id FROM transfer_order WHERE term_id = ?"
    params: list[Any] = [term_id]
    if school_id is not None:
        query += " AND (payer_school_id = ? OR payee_school_id = ?)"
        params.extend([school_id, school_id])
    query += " ORDER BY created_at, transfer_id"
    rows = conn.execute(query, params).fetchall()
    return {
        "term_id": term_id,
        "transfers": [_transfer_payload(conn, row["transfer_id"]) for row in rows],
    }


def create_receipt(
    conn: sqlite3.Connection, transfer_id: str, data: ReceiptIn
) -> tuple[dict[str, Any], bool]:
    """Record an offline arrival against a transfer; returns (payload, created)."""
    with write_transaction(conn):
        transfer = _get_transfer(conn, transfer_id)
        existing = conn.execute(
            "SELECT * FROM transfer_receipt WHERE receipt_id = ?", (data.receipt_id,)
        ).fetchone()
        if existing is not None:
            same_content = (
                existing["transfer_id"] == transfer_id
                and existing["amount_cents"] == data.amount_cents
            )
            if same_content:
                return _transfer_payload(conn, transfer_id), False
            raise ServiceError(
                409,
                "receipt_id_conflict",
                f"receipt id {data.receipt_id} already exists with different content",
            )
        if transfer["payee_school_id"] != data.payee_school_id:
            raise ServiceError(
                403,
                "not_transfer_payee",
                "only the payee school may record receipts for this transfer",
            )
        if transfer["status"] != TRANSFER_OPEN:
            raise ServiceError(
                409,
                "transfer_closed",
                "the payee has confirmed no more arrivals for this transfer",
            )
        received_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transfer_receipt "
            "WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()["total"]
        if received_total + data.amount_cents > transfer["amount_cents"]:
            raise ServiceError(
                409,
                "receipt_exceeds_face",
                "receipts for one transfer cannot exceed its face amount",
                details={
                    "amount_cents": transfer["amount_cents"],
                    "received_cents": received_total,
                },
            )
        conn.execute(
            """
            INSERT INTO transfer_receipt(receipt_id, transfer_id, amount_cents,
                                         recorded_by, received_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                data.receipt_id,
                transfer_id,
                data.amount_cents,
                data.payee_school_id,
                now_iso(),
            ),
        )
        return _transfer_payload(conn, transfer_id), True


def close_transfer(
    conn: sqlite3.Connection, transfer_id: str, payee_school_id: str
) -> dict[str, Any]:
    """Payee confirms no more money will arrive for this transfer.

    The still-outstanding part is released from in-flight totals; arrived
    amounts are kept. Closing an already-closed transfer returns it as-is.
    """
    with write_transaction(conn):
        transfer = _get_transfer(conn, transfer_id)
        if transfer["payee_school_id"] != payee_school_id:
            raise ServiceError(
                403,
                "not_transfer_payee",
                "only the payee school may close this transfer",
            )
        if transfer["status"] == TRANSFER_OPEN:
            conn.execute(
                "UPDATE transfer_order SET status = ?, closed_at = ? "
                "WHERE transfer_id = ?",
                (TRANSFER_CLOSED, now_iso(), transfer_id),
            )
        return _transfer_payload(conn, transfer_id)


# ---------------------------------------------------------------------------
# Payable amounts and per-school balance views
# ---------------------------------------------------------------------------


def get_payable(
    conn: sqlite3.Connection, term_id: str, payer_school_id: str, payee_school_id: str
) -> dict[str, Any]:
    """Current payable amount for one ordered school pair.

    The payable amount is the pair's actual debt (closed allocations and
    arrived money netted in both directions) minus same-direction in-flight
    money; while reverse-direction money may still arrive it is zero.
    """
    with write_transaction(conn):
        _get_term(conn, term_id)
        if payer_school_id == payee_school_id:
            raise ServiceError(
                409, "same_school", "payer and payee must be different schools"
            )
        _require_roster_member(conn, term_id, payer_school_id)
        _require_roster_member(conn, term_id, payee_school_id)
        charges, received, inflight = _pair_components(conn, term_id)
        state = _pair_state(charges, received, inflight, payer_school_id, payee_school_id)
        view = _payable_from_state(state)
        return {
            "term_id": term_id,
            "payer_school_id": payer_school_id,
            "payee_school_id": payee_school_id,
            "debt": _debt_view(state["net_xy"], payer_school_id, payee_school_id),
            "charges_forward_cents": state["charges_xy"],
            "charges_reverse_cents": state["charges_yx"],
            "received_forward_cents": state["received_xy"],
            "received_reverse_cents": state["received_yx"],
            **view,
        }


def get_school_balances(
    conn: sqlite3.Connection, term_id: str, school_id: str
) -> dict[str, Any]:
    """One roster school's live position against every other roster school."""
    with write_transaction(conn):
        _get_term(conn, term_id)
        _require_roster_member(conn, term_id, school_id)
        charges, received, inflight = _pair_components(conn, term_id)
        counterparts = [
            row["school_id"]
            for row in _roster_rows(conn, term_id)
            if row["school_id"] != school_id
        ]
        entries = []
        for other in counterparts:
            state = _pair_state(charges, received, inflight, school_id, other)
            forward = _payable_from_state(state)
            reverse_state = _pair_state(charges, received, inflight, other, school_id)
            reverse = _payable_from_state(reverse_state)
            entries.append(
                {
                    "counterpart_school_id": other,
                    "debt": _debt_view(state["net_xy"], school_id, other),
                    "charges_out_cents": state["charges_xy"],
                    "charges_in_cents": state["charges_yx"],
                    "received_out_cents": state["received_xy"],
                    "received_in_cents": state["received_yx"],
                    "inflight_out_cents": state["inflight_xy"],
                    "inflight_in_cents": state["inflight_yx"],
                    "payable_out_cents": forward["payable_cents"],
                    "payable_in_cents": reverse["payable_cents"],
                }
            )
        return {"term_id": term_id, "school_id": school_id, "counterparts": entries}
