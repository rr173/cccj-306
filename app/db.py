"""SQLite connection and schema management for the inter-school backend."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Union

DEFAULT_DB_PATH = os.environ.get("MICROCERT_DB", "microcred.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS school (
    school_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS student (
    student_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_record (
    record_code             TEXT PRIMARY KEY,
    source_school_id        TEXT NOT NULL REFERENCES school(school_id),
    student_id              TEXT NOT NULL REFERENCES student(student_id),
    category                TEXT NOT NULL,
    points                  INTEGER NOT NULL CHECK (points >= 0),
    published_at            TEXT NOT NULL,
    revoked_at              TEXT,
    revocation_reason       TEXT,
    revocation_school_id    TEXT REFERENCES school(school_id)
);

CREATE TRIGGER IF NOT EXISTS trg_credit_record_immutable_values
BEFORE UPDATE OF source_school_id, student_id, category, points
ON credit_record
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'published credit record values are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_credit_record_no_delete
BEFORE DELETE ON credit_record
BEGIN
    SELECT RAISE(ABORT, 'published credit records cannot be deleted; revoke instead');
END;

CREATE INDEX IF NOT EXISTS idx_credit_record_student
ON credit_record(student_id);

CREATE INDEX IF NOT EXISTS idx_credit_record_source_school
ON credit_record(source_school_id);

CREATE TABLE IF NOT EXISTS credential_definition (
    definition_id       TEXT PRIMARY KEY,
    issuer_school_id    TEXT NOT NULL REFERENCES school(school_id),
    title               TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_item (
    definition_id       TEXT NOT NULL REFERENCES credential_definition(definition_id),
    requirement_code    TEXT NOT NULL,
    position            INTEGER NOT NULL,
    name                TEXT,
    min_points          INTEGER NOT NULL CHECK (min_points >= 0),
    -- whole_record: one record must itself reach min_points.
    -- sum_points: several whole records may be assigned to this item and their
    --             points are summed; a record still cannot be split across items.
    points_mode         TEXT NOT NULL DEFAULT 'whole_record'
                        CHECK (points_mode IN ('whole_record', 'sum_points')),
    PRIMARY KEY (definition_id, requirement_code),
    UNIQUE (definition_id, position)
);

CREATE TABLE IF NOT EXISTS requirement_category (
    definition_id       TEXT NOT NULL,
    requirement_code    TEXT NOT NULL,
    category            TEXT NOT NULL,
    PRIMARY KEY (definition_id, requirement_code, category),
    FOREIGN KEY (definition_id, requirement_code)
        REFERENCES requirement_item(definition_id, requirement_code)
);

CREATE TABLE IF NOT EXISTS certificate (
    certificate_code            TEXT PRIMARY KEY,
    definition_id               TEXT NOT NULL REFERENCES credential_definition(definition_id),
    issuer_school_id            TEXT NOT NULL REFERENCES school(school_id),
    student_id                  TEXT NOT NULL REFERENCES student(student_id),
    status                      TEXT NOT NULL CHECK (
                                    status IN (
                                        'active',
                                        'revocation_pending',
                                        'revocation_processed'
                                    )
                                ),
    issued_at                   TEXT NOT NULL,
    pending_since               TEXT,
    processed_at                TEXT,
    confirmed_by_school_id      TEXT REFERENCES school(school_id)
);

CREATE INDEX IF NOT EXISTS idx_certificate_issuer_status
ON certificate(issuer_school_id, status);

CREATE INDEX IF NOT EXISTS idx_certificate_student
ON certificate(student_id);

CREATE TABLE IF NOT EXISTS certificate_item_record (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_code    TEXT NOT NULL REFERENCES certificate(certificate_code),
    definition_id       TEXT NOT NULL,
    requirement_code    TEXT NOT NULL,
    record_code         TEXT NOT NULL REFERENCES credit_record(record_code),
    category_snapshot   TEXT NOT NULL,
    points_snapshot     INTEGER NOT NULL,
    assigned_at         TEXT NOT NULL,
    released_at         TEXT,
    UNIQUE (certificate_code, requirement_code, record_code),
    FOREIGN KEY (definition_id, requirement_code)
        REFERENCES requirement_item(definition_id, requirement_code)
);

-- A source record may have historical released rows, but at most one
-- unreleased occupancy row. This is the database-level double-use guard.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_record_occupancy
ON certificate_item_record(record_code)
WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_item_record_record
ON certificate_item_record(record_code);

-- ---------------------------------------------------------------------------
-- Advanced practical training: terms, classes, enrollments and joint swaps
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS training_term (
    term_id                 TEXT PRIMARY KEY,
    organizer_school_id     TEXT NOT NULL REFERENCES school(school_id),
    title                   TEXT NOT NULL,
    created_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS term_accepted_credential (
    term_id                 TEXT NOT NULL REFERENCES training_term(term_id),
    definition_id           TEXT NOT NULL REFERENCES credential_definition(definition_id),
    created_at              TEXT NOT NULL,
    PRIMARY KEY (term_id, definition_id)
);

CREATE TABLE IF NOT EXISTS training_class (
    class_id                TEXT PRIMARY KEY,
    term_id                 TEXT NOT NULL REFERENCES training_term(term_id),
    name                    TEXT NOT NULL,
    capacity                INTEGER NOT NULL CHECK (capacity >= 0),
    created_at              TEXT NOT NULL,
    UNIQUE (term_id, name)
);

CREATE INDEX IF NOT EXISTS idx_training_class_term
ON training_class(term_id);

CREATE TABLE IF NOT EXISTS enrollment (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id                 TEXT NOT NULL REFERENCES training_term(term_id),
    student_id              TEXT NOT NULL REFERENCES student(student_id),
    class_id                TEXT NOT NULL REFERENCES training_class(class_id),
    certificate_code        TEXT NOT NULL REFERENCES certificate(certificate_code),
    definition_id           TEXT NOT NULL REFERENCES credential_definition(definition_id),
    status                  TEXT NOT NULL CHECK (
                                status IN ('enrolled', 'withdrawn', 'disqualified')
                            ),
    enrolled_at             TEXT NOT NULL,
    ended_at                TEXT
);

-- A student holds at most one formal seat per term. Withdrawn/disqualified
-- rows remain as history but no longer participate in this unique constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_enrollment
ON enrollment(term_id, student_id)
WHERE status = 'enrolled';

CREATE INDEX IF NOT EXISTS idx_enrollment_active_class
ON enrollment(class_id)
WHERE status = 'enrolled';

CREATE TABLE IF NOT EXISTS swap_order (
    order_id                TEXT PRIMARY KEY,
    term_id                 TEXT NOT NULL REFERENCES training_term(term_id),
    organizer_school_id     TEXT NOT NULL REFERENCES school(school_id),
    status                  TEXT NOT NULL CHECK (
                                status IN (
                                    'pending_confirmation',
                                    'swapped',
                                    'rejected',
                                    'withdrawn',
                                    'disqualified',
                                    'expired'
                                )
                            ),
    confirm_deadline        TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    completed_at            TEXT,
    end_reason              TEXT
);

CREATE INDEX IF NOT EXISTS idx_swap_order_term_status
ON swap_order(term_id, status);

CREATE TABLE IF NOT EXISTS swap_participant (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id                TEXT NOT NULL REFERENCES swap_order(order_id),
    student_id              TEXT NOT NULL,
    from_class_id           TEXT NOT NULL,
    to_class_id             TEXT NOT NULL,
    -- NULL means the student has not responded yet.
    response                TEXT CHECK (response IN ('confirmed', 'rejected')),
    responded_at            TEXT,
    -- 1 while the parent order is still pending_confirmation; flipped to 0
    -- when the order ends for any reason so the partial unique index below
    -- only covers currently open orders.
    order_open              INTEGER NOT NULL DEFAULT 1,
    UNIQUE (order_id, student_id)
);

-- A student cannot belong to two open swap orders at the same time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_swap_student
ON swap_participant(student_id)
WHERE order_open = 1;

CREATE INDEX IF NOT EXISTS idx_swap_participant_order
ON swap_participant(order_id);

-- ---------------------------------------------------------------------------
-- Shared equipment room: physical items, suspensions, class reservations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS equipment_item (
    item_id             TEXT PRIMARY KEY,
    owner_school_id     TEXT NOT NULL REFERENCES school(school_id),
    name                TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equipment_item_owner
ON equipment_item(owner_school_id);

CREATE TABLE IF NOT EXISTS equipment_item_tag (
    item_id             TEXT NOT NULL REFERENCES equipment_item(item_id),
    tag                 TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);

CREATE TABLE IF NOT EXISTS equipment_suspension (
    suspension_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             TEXT NOT NULL REFERENCES equipment_item(item_id),
    owner_school_id     TEXT NOT NULL REFERENCES school(school_id),
    start_at            TEXT NOT NULL,
    end_at              TEXT NOT NULL,
    reason              TEXT,
    created_at          TEXT NOT NULL,
    lifted_at           TEXT,
    CHECK (start_at < end_at)
);

CREATE INDEX IF NOT EXISTS idx_equipment_suspension_item
ON equipment_suspension(item_id);

CREATE TABLE IF NOT EXISTS equipment_reservation (
    seq                     INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id          TEXT NOT NULL UNIQUE,
    organizer_school_id     TEXT NOT NULL REFERENCES school(school_id),
    term_id                 TEXT NOT NULL REFERENCES training_term(term_id),
    class_id                TEXT NOT NULL REFERENCES training_class(class_id),
    start_at                TEXT NOT NULL,
    end_at                  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (
                                status IN (
                                    'accepted',
                                    'picked_up',
                                    'returned',
                                    'cancelled',
                                    'expired'
                                )
                            ),
    created_at              TEXT NOT NULL,
    accepted_at             TEXT NOT NULL,
    picked_up_at            TEXT,
    returned_at             TEXT,
    cancelled_at            TEXT,
    ended_at                TEXT,
    CHECK (start_at < end_at)
);

CREATE INDEX IF NOT EXISTS idx_equipment_reservation_organizer
ON equipment_reservation(organizer_school_id, status);

CREATE INDEX IF NOT EXISTS idx_equipment_reservation_class
ON equipment_reservation(term_id, class_id);

CREATE TABLE IF NOT EXISTS equipment_reservation_requirement (
    requirement_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id      TEXT NOT NULL REFERENCES equipment_reservation(reservation_id),
    position            INTEGER NOT NULL,
    count               INTEGER NOT NULL CHECK (count >= 1),
    UNIQUE (reservation_id, position)
);

CREATE TABLE IF NOT EXISTS equipment_requirement_tag (
    requirement_id      INTEGER NOT NULL
                        REFERENCES equipment_reservation_requirement(requirement_id),
    tag                 TEXT NOT NULL,
    PRIMARY KEY (requirement_id, tag)
);

CREATE TABLE IF NOT EXISTS equipment_allocation (
    allocation_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id      TEXT NOT NULL REFERENCES equipment_reservation(reservation_id),
    requirement_id      INTEGER NOT NULL
                        REFERENCES equipment_reservation_requirement(requirement_id),
    item_id             TEXT NOT NULL REFERENCES equipment_item(item_id),
    planned_at          TEXT NOT NULL,
    picked_up_at        TEXT,
    returned_at         TEXT,
    UNIQUE (reservation_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_equipment_allocation_item
ON equipment_allocation(item_id);

CREATE INDEX IF NOT EXISTS idx_equipment_allocation_reservation
ON equipment_allocation(reservation_id);

-- At most one unreleased physical handover of an item may exist at any time.
-- Planned (not yet handed over) rows may be shared by non-overlapping bookings;
-- this index is the database-level guard against double lending.
CREATE UNIQUE INDEX IF NOT EXISTS uq_equipment_active_handover
ON equipment_allocation(item_id)
WHERE picked_up_at IS NOT NULL AND returned_at IS NULL;

-- ---------------------------------------------------------------------------
-- Cross-school reconciliation: share rosters, advanced repair expenses,
-- statement rounds, transfer orders and offline receipts
-- ---------------------------------------------------------------------------

-- Per-term share roster registered by the organizing school. Frozen as soon
-- as the first expense is reported for the term.
CREATE TABLE IF NOT EXISTS share_roster (
    term_id     TEXT NOT NULL REFERENCES training_term(term_id),
    school_id   TEXT NOT NULL REFERENCES school(school_id),
    share       INTEGER NOT NULL CHECK (share >= 1),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (term_id, school_id)
);

-- One advanced expense per borrowed physical item per borrowing (reservation).
CREATE TABLE IF NOT EXISTS shared_expense (
    expense_id          TEXT PRIMARY KEY,
    term_id             TEXT NOT NULL REFERENCES training_term(term_id),
    reservation_id      TEXT NOT NULL REFERENCES equipment_reservation(reservation_id),
    item_id             TEXT NOT NULL REFERENCES equipment_item(item_id),
    advancing_school_id TEXT NOT NULL REFERENCES school(school_id),
    created_at          TEXT NOT NULL,
    UNIQUE (reservation_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_shared_expense_term
ON shared_expense(term_id);

-- Every expense is a chain of versions: the original claim plus corrections.
-- A version takes effect only once the organizer approves it, and is booked
-- into a statement round at close time (round_no stays NULL until then).
CREATE TABLE IF NOT EXISTS expense_version (
    version_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id       TEXT NOT NULL REFERENCES shared_expense(expense_id),
    version_no       INTEGER NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    replaces_version INTEGER,
    status           TEXT NOT NULL CHECK (status IN ('pending', 'approved')),
    submitted_by     TEXT NOT NULL REFERENCES school(school_id),
    submitted_at     TEXT NOT NULL,
    approved_by      TEXT REFERENCES school(school_id),
    approved_at      TEXT,
    round_no         INTEGER,
    UNIQUE (expense_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_expense_version_expense
ON expense_version(expense_id);

-- Full per-school allocation of one expense version (largest-remainder split
-- of amount_cents across the frozen roster). Corrections are booked later as
-- the delta between two versions' full allocations.
CREATE TABLE IF NOT EXISTS expense_allocation (
    version_id  INTEGER NOT NULL REFERENCES expense_version(version_id),
    school_id   TEXT NOT NULL REFERENCES school(school_id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    PRIMARY KEY (version_id, school_id)
);

-- One closed statement round per (term, round_no). Rows are immutable once
-- written; closing the same round again simply returns the stored statement.
CREATE TABLE IF NOT EXISTS statement (
    term_id     TEXT NOT NULL REFERENCES training_term(term_id),
    round_no    INTEGER NOT NULL CHECK (round_no >= 1),
    closed_by   TEXT NOT NULL REFERENCES school(school_id),
    closed_at   TEXT NOT NULL,
    PRIMARY KEY (term_id, round_no)
);

CREATE TABLE IF NOT EXISTS statement_line (
    term_id     TEXT NOT NULL,
    round_no    INTEGER NOT NULL,
    expense_id  TEXT NOT NULL REFERENCES shared_expense(expense_id),
    version_no  INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    PRIMARY KEY (term_id, round_no, expense_id, version_no),
    FOREIGN KEY (term_id, round_no) REFERENCES statement(term_id, round_no)
);

-- Booked delta per school for one statement line (full allocation for an
-- original claim, new-minus-old allocation for a correction; may be <= 0).
CREATE TABLE IF NOT EXISTS statement_line_allocation (
    term_id     TEXT NOT NULL,
    round_no    INTEGER NOT NULL,
    expense_id  TEXT NOT NULL,
    version_no  INTEGER NOT NULL,
    school_id   TEXT NOT NULL REFERENCES school(school_id),
    delta_cents INTEGER NOT NULL,
    PRIMARY KEY (term_id, round_no, expense_id, version_no, school_id),
    FOREIGN KEY (term_id, round_no, expense_id, version_no)
        REFERENCES statement_line(term_id, round_no, expense_id, version_no)
);

-- Point-in-time snapshot of one school pair taken at close time. school_a is
-- the lexicographically smaller school id; amounts suffixed _a_to_b describe
-- that direction. net_a_to_b > 0 means school_a owed school_b at close time.
CREATE TABLE IF NOT EXISTS statement_pair (
    term_id         TEXT NOT NULL,
    round_no        INTEGER NOT NULL,
    school_a        TEXT NOT NULL REFERENCES school(school_id),
    school_b        TEXT NOT NULL REFERENCES school(school_id),
    charges_a_to_b  INTEGER NOT NULL,
    charges_b_to_a  INTEGER NOT NULL,
    received_a_to_b INTEGER NOT NULL,
    received_b_to_a INTEGER NOT NULL,
    inflight_a_to_b INTEGER NOT NULL,
    inflight_b_to_a INTEGER NOT NULL,
    net_a_to_b      INTEGER NOT NULL,
    PRIMARY KEY (term_id, round_no, school_a, school_b),
    FOREIGN KEY (term_id, round_no) REFERENCES statement(term_id, round_no)
);

-- A transfer order registered by the paying school. The payee records offline
-- receipts against it and finally confirms no more money will arrive, which
-- releases the still-outstanding part from the in-flight totals.
CREATE TABLE IF NOT EXISTS transfer_order (
    transfer_id     TEXT PRIMARY KEY,
    term_id         TEXT NOT NULL REFERENCES training_term(term_id),
    payer_school_id TEXT NOT NULL REFERENCES school(school_id),
    payee_school_id TEXT NOT NULL REFERENCES school(school_id),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    status          TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfer_order_term
ON transfer_order(term_id, status);

CREATE TABLE IF NOT EXISTS transfer_receipt (
    receipt_id  TEXT PRIMARY KEY,
    transfer_id TEXT NOT NULL REFERENCES transfer_order(transfer_id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    recorded_by TEXT NOT NULL REFERENCES school(school_id),
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfer_receipt_transfer
ON transfer_receipt(transfer_id);
"""


def connect(db_path: Union[str, Path, None] = None) -> sqlite3.Connection:
    """Open a SQLite connection configured for request/worker use."""
    path = str(db_path if db_path is not None else DEFAULT_DB_PATH)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Foreign keys are connection-scoped in SQLite.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        # WAL lets external verification readers proceed while a writer commits.
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: Union[str, Path, None] = None) -> None:
    """Create the schema if needed."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def reset_db_file(db_path: Union[str, Path]) -> None:
    """Remove a file-backed database and initialize an empty schema."""
    path = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(path) + suffix)
        try:
            side.unlink()
        except FileNotFoundError:
            pass
    init_db(path)
