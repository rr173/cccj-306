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
