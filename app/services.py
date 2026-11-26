"""Transaction-safe domain services for schools, records, review and issuance."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from .matching import find_allocation
from .models import DefinitionIn, IssueIn

STATUS_ACTIVE = "active"
STATUS_PENDING = "revocation_pending"
STATUS_PROCESSED = "revocation_processed"

STATUS_LABELS_ZH = {
    STATUS_ACTIVE: "仍有效",
    STATUS_PENDING: "待处理撤销",
    STATUS_PROCESSED: "已处理撤销",
}

STATUS_FILTER_ALIASES = {
    "active": STATUS_ACTIVE,
    "valid": STATUS_ACTIVE,
    STATUS_ACTIVE: STATUS_ACTIVE,
    "pending": STATUS_PENDING,
    "pending_revocation": STATUS_PENDING,
    STATUS_PENDING: STATUS_PENDING,
    "revoked": STATUS_PROCESSED,
    "processed": STATUS_PROCESSED,
    STATUS_PROCESSED: STATUS_PROCESSED,
}


class ServiceError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """All writes take the SQLite global write lock before reading state."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Basic local directory data
# ---------------------------------------------------------------------------


def create_school(conn: sqlite3.Connection, school_id: str, name: str) -> dict[str, Any]:
    ts = now_iso()
    try:
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO school(school_id, name, created_at) VALUES (?, ?, ?)",
                (school_id, name, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(409, "school_exists", f"school {school_id} already exists") from exc
    return get_school(conn, school_id)


def list_schools(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM school ORDER BY school_id").fetchall()
    return [dict(row) for row in rows]


def get_school(conn: sqlite3.Connection, school_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM school WHERE school_id = ?", (school_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "school_not_found", f"school {school_id} was not found")
    return dict(row)


def create_student(conn: sqlite3.Connection, student_id: str, name: str) -> dict[str, Any]:
    ts = now_iso()
    try:
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO student(student_id, name, created_at) VALUES (?, ?, ?)",
                (student_id, name, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409, "student_exists", f"student {student_id} already exists"
        ) from exc
    return get_student(conn, student_id)


def get_student(conn: sqlite3.Connection, student_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM student WHERE student_id = ?", (student_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "student_not_found", f"student {student_id} was not found")
    return dict(row)


def create_record(
    conn: sqlite3.Connection,
    record_code: str,
    source_school_id: str,
    student_id: str,
    category: str,
    points: int,
) -> dict[str, Any]:
    # Explicit checks give callers actionable 404s rather than FK IntegrityErrors.
    get_school(conn, source_school_id)
    get_student(conn, student_id)
    ts = now_iso()
    try:
        with write_transaction(conn):
            conn.execute(
                """
                INSERT INTO credit_record(
                    record_code, source_school_id, student_id, category, points, published_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_code, source_school_id, student_id, category, points, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409, "record_exists", f"record {record_code} already exists"
        ) from exc
    return get_record(conn, record_code)


def get_record(conn: sqlite3.Connection, record_code: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM credit_record WHERE record_code = ?", (record_code,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "record_not_found", f"record {record_code} was not found")
    return record_row_to_dict(row)


def record_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "record_code": row["record_code"],
        "source_school_id": row["source_school_id"],
        "student_id": row["student_id"],
        "category": row["category"],
        "points": row["points"],
        "published_at": row["published_at"],
        "revoked_at": row["revoked_at"],
        "revocation_reason": row["revocation_reason"],
        "revocation_school_id": row["revocation_school_id"],
        "revoked": row["revoked_at"] is not None,
    }


def list_records(
    conn: sqlite3.Connection,
    student_id: Optional[str] = None,
    source_school_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if student_id is not None:
        clauses.append("student_id = ?")
        params.append(student_id)
    if source_school_id is not None:
        clauses.append("source_school_id = ?")
        params.append(source_school_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM credit_record{where} ORDER BY record_code", params
    ).fetchall()
    return [record_row_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Credential definitions / requirement items
# ---------------------------------------------------------------------------


def create_definition(conn: sqlite3.Connection, data: DefinitionIn) -> dict[str, Any]:
    get_school(conn, data.issuer_school_id)
    ts = now_iso()
    try:
        with write_transaction(conn):
            conn.execute(
                """
                INSERT INTO credential_definition(definition_id, issuer_school_id, title, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (data.definition_id, data.issuer_school_id, data.title, ts),
            )
            for position, req in enumerate(data.requirements):
                conn.execute(
                    """
                    INSERT INTO requirement_item(
                        definition_id, requirement_code, position, name, min_points, points_mode
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.definition_id,
                        req.requirement_code,
                        position,
                        req.name,
                        req.min_points,
                        req.points_mode,
                    ),
                )
                for category in req.accepted_categories:
                    conn.execute(
                        """
                        INSERT INTO requirement_category(
                            definition_id, requirement_code, category
                        ) VALUES (?, ?, ?)
                        """,
                        (data.definition_id, req.requirement_code, category),
                    )
    except sqlite3.IntegrityError as exc:
        raise ServiceError(
            409,
            "definition_exists",
            f"credential definition {data.definition_id} already exists or references invalid data",
        ) from exc
    return get_definition(conn, data.definition_id)


def get_definition_bundle(conn: sqlite3.Connection, definition_id: str) -> dict[str, Any]:
    definition_row = conn.execute(
        "SELECT * FROM credential_definition WHERE definition_id = ?",
        (definition_id,),
    ).fetchone()
    if definition_row is None:
        raise ServiceError(
            404,
            "definition_not_found",
            f"credential definition {definition_id} was not found",
        )

    requirement_rows = conn.execute(
        """
        SELECT * FROM requirement_item
        WHERE definition_id = ?
        ORDER BY position, requirement_code
        """,
        (definition_id,),
    ).fetchall()
    category_rows = conn.execute(
        """
        SELECT requirement_code, category
        FROM requirement_category
        WHERE definition_id = ?
        ORDER BY requirement_code, category
        """,
        (definition_id,),
    ).fetchall()

    categories_by_req: dict[str, list[str]] = {}
    for row in category_rows:
        categories_by_req.setdefault(row["requirement_code"], []).append(row["category"])

    requirements = [
        {
            "requirement_code": row["requirement_code"],
            "name": row["name"],
            "position": row["position"],
            "min_points": row["min_points"],
            "points_mode": row["points_mode"],
            "accepted_categories": categories_by_req.get(row["requirement_code"], []),
        }
        for row in requirement_rows
    ]
    return {"definition": dict(definition_row), "requirements": requirements}


def get_definition(conn: sqlite3.Connection, definition_id: str) -> dict[str, Any]:
    bundle = get_definition_bundle(conn, definition_id)
    definition = bundle["definition"]
    definition["requirements"] = bundle["requirements"]
    return definition


def list_definitions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT definition_id FROM credential_definition ORDER BY definition_id"
    ).fetchall()
    return [get_definition(conn, row["definition_id"]) for row in rows]


# ---------------------------------------------------------------------------
# Candidate status and non-occupying review
# ---------------------------------------------------------------------------


def _candidate_report(
    conn: sqlite3.Connection,
    student_id: str,
    record_codes: list[str],
    requirements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, sqlite3.Row]]:
    if not record_codes:
        return [], {}, {}

    placeholders = ",".join("?" for _ in record_codes)
    record_rows = conn.execute(
        f"SELECT * FROM credit_record WHERE record_code IN ({placeholders})",
        record_codes,
    ).fetchall()
    row_by_code = {row["record_code"]: row for row in record_rows}

    occupancy_rows = conn.execute(
        f"""
        SELECT o.record_code,
               o.certificate_code,
               o.requirement_code,
               c.status AS certificate_status,
               c.issuer_school_id
        FROM certificate_item_record o
        JOIN certificate c ON c.certificate_code = o.certificate_code
        WHERE o.released_at IS NULL
          AND o.record_code IN ({placeholders})
        """,
        record_codes,
    ).fetchall()
    occupancy_by_record = {row["record_code"]: row for row in occupancy_rows}

    candidates: list[dict[str, Any]] = []
    options_by_record: dict[str, list[str]] = {}

    for code in record_codes:
        row = row_by_code.get(code)
        if row is None:
            candidates.append(
                {
                    "record_code": code,
                    "found": False,
                    "status": "not_found",
                    "usable": False,
                    "eligible_requirement_codes": [],
                    "assignable_requirement_codes": [],
                    "category_accepted_requirement_codes": [],
                    "occupancy": None,
                }
            )
            continue

        category_accepted_codes = [
            req["requirement_code"]
            for req in requirements
            if row["category"] in req["accepted_categories"]
        ]
        threshold_codes = [
            req["requirement_code"]
            for req in requirements
            if row["category"] in req["accepted_categories"]
            and row["points"] >= req["min_points"]
        ]
        assignable_codes = [
            req["requirement_code"]
            for req in requirements
            if row["category"] in req["accepted_categories"]
            and (
                req.get("points_mode", "whole_record") == "sum_points"
                or row["points"] >= req["min_points"]
            )
        ]
        occ_row = occupancy_by_record.get(code)
        occupancy = (
            {
                "certificate_code": occ_row["certificate_code"],
                "requirement_code": occ_row["requirement_code"],
                "certificate_status": occ_row["certificate_status"],
                "certificate_status_zh": STATUS_LABELS_ZH[occ_row["certificate_status"]],
                "issuer_school_id": occ_row["issuer_school_id"],
            }
            if occ_row is not None
            else None
        )

        belongs_to_student = row["student_id"] == student_id
        revoked = row["revoked_at"] is not None

        if not belongs_to_student:
            status = "wrong_student"
        elif revoked:
            status = "revoked"
        elif occupancy is not None:
            status = "occupied"
        elif not assignable_codes:
            status = "ineligible"
        else:
            status = "available"

        usable = status == "available"
        candidate = {
            **record_row_to_dict(row),
            "found": True,
            "belongs_to_student": belongs_to_student,
            "status": status,
            "status_zh": {
                "available": "可用",
                "ineligible": "课程类别或分值不满足任何要求项",
                "occupied": "已被未释放占用的证书使用",
                "revoked": "来源学校已撤销",
                "wrong_student": "记录不属于该学生",
                "not_found": "记录不存在",
            }[status],
            "usable": usable,
            # Backwards-compatible name: independently reaches the item minimum.
            "eligible_requirement_codes": threshold_codes,
            "single_record_qualifies_requirement_codes": threshold_codes,
            "category_accepted_requirement_codes": category_accepted_codes,
            # Under sum_points, a category-accepted lower-point record is assignable.
            "assignable_requirement_codes": assignable_codes,
            "occupancy": occupancy,
        }
        candidates.append(candidate)

        if usable:
            options_by_record[code] = assignable_codes

    return candidates, options_by_record, row_by_code


def _solve_from_candidates(
    requirements: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    options_by_record: dict[str, list[str]],
) -> Optional[dict[str, list[str]]]:
    usable = [candidate for candidate in candidates if candidate["usable"]]
    points_by_record = {candidate["record_code"]: candidate["points"] for candidate in usable}
    return find_allocation(requirements, points_by_record, options_by_record)


def _assignment_payload(
    requirements: list[dict[str, Any]],
    assignment: Optional[dict[str, list[str]]],
    row_by_code: dict[str, sqlite3.Row],
) -> list[dict[str, Any]]:
    if assignment is None:
        return []
    result = []
    for req in requirements:
        req_code = req["requirement_code"]
        record_codes = list(assignment.get(req_code, []))
        records = [
            {
                "record_code": record_code,
                "category": row_by_code[record_code]["category"],
                "points": row_by_code[record_code]["points"],
            }
            for record_code in record_codes
        ]
        assigned_points = sum(item["points"] for item in records)
        mode = req.get("points_mode", "whole_record")
        satisfied = (
            len(record_codes) == 1
            if mode == "whole_record"
            else assigned_points >= req["min_points"]
        )
        result.append(
            {
                "requirement_code": req_code,
                "requirement_name": req.get("name"),
                "points_mode": mode,
                "min_points": req["min_points"],
                "accepted_categories": req["accepted_categories"],
                "record_codes": record_codes,
                # Convenience for the common one-record-per-item response.
                "record_code": record_codes[0] if len(record_codes) == 1 else None,
                "records": records,
                "assigned_points": assigned_points,
                "satisfied": satisfied,
            }
        )
    return result


def review_definition(
    conn: sqlite3.Connection,
    definition_id: str,
    student_id: str,
    record_codes: list[str],
) -> dict[str, Any]:
    student = get_student(conn, student_id)
    bundle = get_definition_bundle(conn, definition_id)
    definition = bundle["definition"]
    requirements = bundle["requirements"]

    candidates, options_by_record, row_by_code = _candidate_report(
        conn, student_id, record_codes, requirements
    )
    assignment = _solve_from_candidates(requirements, candidates, options_by_record)

    feasible = assignment is not None
    assignments = _assignment_payload(requirements, assignment, row_by_code)
    if feasible:
        conclusion = "已找到一个方案：所有要求项达标，且每条整条记录最多分配给一个要求项；预审不占用记录。"
    else:
        conclusion = (
            "无法同时满足全部要求：即使某条记录符合多个要求项，也只能整条分配给其中一项，"
            "候选记录无法在不重复使用、不拆分的情况下覆盖所有要求项。"
        )

    return {
        "definition_id": definition_id,
        "credential_title": definition["title"],
        "issuer_school_id": definition["issuer_school_id"],
        "student_id": student_id,
        "student_name": student["name"],
        "feasible": feasible,
        "assignments": assignments,
        "candidates": candidates,
        "conclusion": conclusion,
        "occupancy_rule": "active 与 revocation_pending 状态证书中的未释放记录均被占用；revocation_processed 后释放。",
    }


# ---------------------------------------------------------------------------
# Explicit issuance with atomic recheck
# ---------------------------------------------------------------------------


def issue_certificate(conn: sqlite3.Connection, data: IssueIn) -> dict[str, Any]:
    # Validate references before taking the write lock where possible.
    get_student(conn, data.student_id)
    bundle = get_definition_bundle(conn, data.definition_id)
    requirements = bundle["requirements"]
    req_by_code = {req["requirement_code"]: req for req in requirements}

    with write_transaction(conn):
        # Re-read candidate state after BEGIN IMMEDIATE. This is the issuance
        # decision's single concurrency-safe recheck.
        candidates, options_by_record, row_by_code = _candidate_report(
            conn, data.student_id, data.record_codes, requirements
        )
        review_report = _build_review_report(
            bundle,
            data.student_id,
            candidates,
            options_by_record,
            row_by_code,
        )

        assignment: Optional[dict[str, list[str]]]
        if data.assignments is None:
            assignment = _solve_from_candidates(
                requirements, candidates, options_by_record
            )
            if assignment is None:
                raise ServiceError(
                    409,
                    "no_feasible_assignment",
                    "cannot issue: no assignment satisfies every requirement without reuse",
                    details={"review": review_report},
                )
        else:
            assignment, errors = _validate_requested_assignments(
                data.assignments,
                requirements,
                candidates,
                req_by_code,
            )
            if errors:
                raise ServiceError(
                    409,
                    "invalid_assignment",
                    "cannot issue: supplied assignment failed atomic recheck",
                    details={"errors": errors, "review": review_report},
                )

        certificate_code = data.certificate_code or f"CERT-{uuid.uuid4().hex[:16].upper()}"
        existing = conn.execute(
            "SELECT 1 FROM certificate WHERE certificate_code = ?",
            (certificate_code,),
        ).fetchone()
        if existing is not None:
            raise ServiceError(
                409,
                "certificate_code_exists",
                f"certificate {certificate_code} already exists",
            )

        ts = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO certificate(
                    certificate_code, definition_id, issuer_school_id, student_id,
                    status, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    certificate_code,
                    data.definition_id,
                    bundle["definition"]["issuer_school_id"],
                    data.student_id,
                    STATUS_ACTIVE,
                    ts,
                ),
            )
            for req in requirements:
                req_code = req["requirement_code"]
                for record_code in assignment.get(req_code, []):
                    record_row = row_by_code[record_code]
                    conn.execute(
                        """
                        INSERT INTO certificate_item_record(
                            certificate_code, definition_id, requirement_code, record_code,
                            category_snapshot, points_snapshot, assigned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            certificate_code,
                            data.definition_id,
                            req_code,
                            record_code,
                            record_row["category"],
                            record_row["points"],
                            ts,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            # Partial unique index: another committed issuance already holds a record.
            raise ServiceError(
                409,
                "record_occupancy_conflict",
                "cannot issue: one or more records were occupied by a concurrent certificate",
                details={"review": review_report},
            ) from exc

        result = get_certificate_detail(conn, certificate_code)

    return result


def _build_review_report(
    bundle: dict[str, Any],
    student_id: str,
    candidates: list[dict[str, Any]],
    options_by_record: dict[str, list[str]],
    row_by_code: dict[str, sqlite3.Row],
) -> dict[str, Any]:
    definition = bundle["definition"]
    requirements = bundle["requirements"]
    assignment = _solve_from_candidates(requirements, candidates, options_by_record)
    return {
        "definition_id": definition["definition_id"],
        "student_id": student_id,
        "feasible": assignment is not None,
        "assignments": _assignment_payload(requirements, assignment, row_by_code),
        "candidates": candidates,
    }


def _validate_requested_assignments(
    requested: list[Any],
    requirements: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    req_by_code: dict[str, dict[str, Any]],
) -> tuple[Optional[dict[str, list[str]]], list[dict[str, Any]]]:
    candidate_by_code = {c["record_code"]: c for c in candidates}
    assignment: dict[str, list[str]] = {req["requirement_code"]: [] for req in requirements}
    used_records: set[str] = set()
    errors: list[dict[str, Any]] = []

    for item in requested:
        req_code = item.requirement_code
        record_code = item.record_code
        req = req_by_code.get(req_code)
        candidate = candidate_by_code.get(record_code)

        if req is None:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": "unknown_requirement",
                }
            )
            continue
        if candidate is None:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": "record_not_in_review_set",
                }
            )
            continue
        if not candidate["usable"]:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": f"record_{candidate['status']}",
                    "candidate_status": candidate["status"],
                    "occupancy": candidate.get("occupancy"),
                }
            )
            continue
        if record_code in used_records:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": "record_already_assigned_to_another_requirement",
                }
            )
            continue
        if req_code not in candidate["assignable_requirement_codes"]:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": "record_does_not_satisfy_requirement_rule",
                    "points_mode": req.get("points_mode", "whole_record"),
                    "min_points": req["min_points"],
                }
            )
            continue
        if record_code in assignment[req_code]:
            errors.append(
                {
                    "requirement_code": req_code,
                    "record_code": record_code,
                    "error": "duplicate_requirement_record_pair",
                }
            )
            continue

        assignment[req_code].append(record_code)
        used_records.add(record_code)

    for req in requirements:
        req_code = req["requirement_code"]
        record_codes = assignment[req_code]
        mode = req.get("points_mode", "whole_record")
        total_points = sum(candidate_by_code[code]["points"] for code in record_codes)

        if mode == "whole_record" and len(record_codes) != 1:
            errors.append(
                {
                    "requirement_code": req_code,
                    "error": "whole_record_requirement_needs_exactly_one_record",
                    "assigned_record_codes": record_codes,
                }
            )
        elif mode == "sum_points" and total_points < req["min_points"]:
            errors.append(
                {
                    "requirement_code": req_code,
                    "error": "assigned_points_below_minimum",
                    "assigned_points": total_points,
                    "min_points": req["min_points"],
                    "assigned_record_codes": record_codes,
                }
            )

    if errors:
        return None, errors
    return assignment, []


# ---------------------------------------------------------------------------
# Certificate queries, verification, revocation lifecycle
# ---------------------------------------------------------------------------


def _effective_status(stored_status: str, revoked_basis_codes: list[str]) -> str:
    if stored_status == STATUS_PROCESSED:
        return STATUS_PROCESSED
    if stored_status == STATUS_PENDING or revoked_basis_codes:
        return STATUS_PENDING
    return STATUS_ACTIVE


def get_certificate_detail(conn: sqlite3.Connection, certificate_code: str) -> dict[str, Any]:
    cert_row = conn.execute(
        "SELECT * FROM certificate WHERE certificate_code = ?",
        (certificate_code,),
    ).fetchone()
    if cert_row is None:
        raise ServiceError(
            404, "certificate_not_found", f"certificate {certificate_code} was not found"
        )

    item_rows = conn.execute(
        """
        SELECT i.*,
               r.name AS requirement_name,
               r.min_points AS requirement_min_points,
               r.points_mode AS requirement_points_mode,
               cr.category AS current_category,
               cr.points AS current_points,
               cr.revoked_at AS record_revoked_at,
               cr.revocation_reason AS record_revocation_reason,
               cr.source_school_id AS record_source_school_id
        FROM certificate_item_record i
        JOIN requirement_item r
          ON r.definition_id = i.definition_id
         AND r.requirement_code = i.requirement_code
        JOIN credit_record cr
          ON cr.record_code = i.record_code
        WHERE i.certificate_code = ?
        ORDER BY r.position, i.requirement_code
        """,
        (certificate_code,),
    ).fetchall()

    items = []
    requirement_results: dict[str, dict[str, Any]] = {}
    revoked_basis_codes: list[str] = []
    for row in item_rows:
        record_revoked = row["record_revoked_at"] is not None
        if record_revoked:
            revoked_basis_codes.append(row["record_code"])
        req_code = row["requirement_code"]
        requirement_results.setdefault(
            req_code,
            {
                "requirement_code": req_code,
                "requirement_name": row["requirement_name"],
                "points_mode": row["requirement_points_mode"],
                "min_points": row["requirement_min_points"],
                "record_codes": [],
                "assigned_points": 0,
            },
        )
        requirement_results[req_code]["record_codes"].append(row["record_code"])
        requirement_results[req_code]["assigned_points"] += row["points_snapshot"]
        items.append(
            {
                "requirement_code": row["requirement_code"],
                "requirement_name": row["requirement_name"],
                "record_code": row["record_code"],
                "category_snapshot": row["category_snapshot"],
                "points_snapshot": row["points_snapshot"],
                "assigned_at": row["assigned_at"],
                "released_at": row["released_at"],
                "still_occupies_record": row["released_at"] is None,
                "source_school_id": row["record_source_school_id"],
                "source_record": {
                    "category": row["current_category"],
                    "points": row["current_points"],
                    "revoked": record_revoked,
                    "revoked_at": row["record_revoked_at"],
                    "revocation_reason": row["record_revocation_reason"],
                },
            }
        )

    requirement_result_list = []
    for result in requirement_results.values():
        mode = result["points_mode"]
        result["satisfied"] = (
            len(result["record_codes"]) == 1
            if mode == "whole_record"
            else result["assigned_points"] >= result["min_points"]
        )
        requirement_result_list.append(result)

    stored_status = cert_row["status"]
    effective_status = _effective_status(stored_status, revoked_basis_codes)
    return {
        "certificate_code": cert_row["certificate_code"],
        "status": stored_status,
        "definition_id": cert_row["definition_id"],
        "issuer_school_id": cert_row["issuer_school_id"],
        "student_id": cert_row["student_id"],
        "issued_at": cert_row["issued_at"],
        "lifecycle_status": stored_status,
        "lifecycle_status_zh": STATUS_LABELS_ZH[stored_status],
        "effective_status": effective_status,
        "effective_status_zh": STATUS_LABELS_ZH[effective_status],
        "valid": effective_status == STATUS_ACTIVE,
        "currently_valid": effective_status == STATUS_ACTIVE,
        "is_valid": effective_status == STATUS_ACTIVE,
        "pending_since": cert_row["pending_since"],
        "processed_at": cert_row["processed_at"],
        "confirmed_by_school_id": cert_row["confirmed_by_school_id"],
        "revoked_basis_record_codes": revoked_basis_codes,
        "requirement_results": requirement_result_list,
        "items": items,
        "basis_preserved": "all item rows remain available after revocation/release",
    }


def verify_certificate(conn: sqlite3.Connection, certificate_code: str) -> dict[str, Any]:
    detail = get_certificate_detail(conn, certificate_code)
    # Verification deliberately recomputes validity from current source records,
    # so a committed source revocation can never verify as valid even if a
    # lifecycle transition were delayed.
    valid = detail["effective_status"] == STATUS_ACTIVE
    reasons = []
    if detail["effective_status"] == STATUS_PENDING:
        reasons.append(
            "at least one source record was revoked; certificate is stopped and awaiting issuer confirmation"
        )
    if detail["effective_status"] == STATUS_PROCESSED:
        reasons.append("issuing school has processed the source revocation and released holds")
    return {
        "certificate_code": certificate_code,
        "valid": valid,
        "status": detail["effective_status"],
        "status_zh": detail["effective_status_zh"],
        "lifecycle_status": detail["lifecycle_status"],
        "issuer_school_id": detail["issuer_school_id"],
        "student_id": detail["student_id"],
        "definition_id": detail["definition_id"],
        "checked_at": now_iso(),
        "revoked_basis_record_codes": detail["revoked_basis_record_codes"],
        "reasons": reasons,
        "items": [
            {
                "requirement_code": item["requirement_code"],
                "record_code": item["record_code"],
                "points_snapshot": item["points_snapshot"],
                "category_snapshot": item["category_snapshot"],
                "source_revoked": item["source_record"]["revoked"],
            }
            for item in detail["items"]
        ],
    }


def get_record_usage(conn: sqlite3.Connection, record_code: str) -> dict[str, Any]:
    record = get_record(conn, record_code)
    rows = conn.execute(
        """
        SELECT c.certificate_code,
               c.status AS certificate_status,
               c.issuer_school_id,
               i.requirement_code,
               i.released_at
        FROM certificate_item_record i
        JOIN certificate c ON c.certificate_code = i.certificate_code
        WHERE i.record_code = ?
        ORDER BY c.issued_at DESC, i.id
        """,
        (record_code,),
    ).fetchall()
    usage = []
    active_hold = None
    for row in rows:
        item = {
            "certificate_code": row["certificate_code"],
            "certificate_status": row["certificate_status"],
            "certificate_status_zh": STATUS_LABELS_ZH[row["certificate_status"]],
            "issuer_school_id": row["issuer_school_id"],
            "requirement_code": row["requirement_code"],
            "released_at": row["released_at"],
            "currently_holds_record": row["released_at"] is None,
        }
        usage.append(item)
        if item["currently_holds_record"] and active_hold is None:
            active_hold = item
    record["usage"] = usage
    record["active_hold"] = active_hold
    return record


def list_student_certificates(
    conn: sqlite3.Connection,
    student_id: str,
) -> dict[str, Any]:
    get_student(conn, student_id)
    rows = conn.execute(
        """
        SELECT certificate_code
        FROM certificate
        WHERE student_id = ?
        ORDER BY issued_at DESC, certificate_code
        """,
        (student_id,),
    ).fetchall()
    return {
        "student_id": student_id,
        "certificates": [get_certificate_detail(conn, row["certificate_code"]) for row in rows],
    }


def list_school_certificates(
    conn: sqlite3.Connection,
    school_id: str,
    status_filter: Optional[str] = None,
) -> dict[str, Any]:
    get_school(conn, school_id)
    if status_filter is not None:
        status_filter = STATUS_FILTER_ALIASES.get(status_filter, status_filter)
    allowed = set(STATUS_LABELS_ZH)
    if status_filter is not None and status_filter not in allowed:
        raise ServiceError(
            400,
            "invalid_status_filter",
            f"status must be one of {sorted(allowed)}",
        )

    rows = conn.execute(
        """
        SELECT certificate_code
        FROM certificate
        WHERE issuer_school_id = ?
          AND (? IS NULL OR status = ?)
        ORDER BY issued_at DESC, certificate_code
        """,
        (school_id, status_filter, status_filter),
    ).fetchall()
    certificates = [get_certificate_detail(conn, row["certificate_code"]) for row in rows]

    count_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM certificate
        WHERE issuer_school_id = ?
        GROUP BY status
        """,
        (school_id,),
    ).fetchall()
    counts = {status: 0 for status in (STATUS_ACTIVE, STATUS_PENDING, STATUS_PROCESSED)}
    for row in count_rows:
        counts[row["status"]] = row["count"]

    return {
        "school_id": school_id,
        "status_filter": status_filter,
        "status_labels_zh": STATUS_LABELS_ZH,
        "counts": counts,
        "certificates": certificates,
    }


def get_school_ledger(conn: sqlite3.Connection, school_id: str) -> dict[str, Any]:
    school = get_school(conn, school_id)
    issued = list_school_certificates(conn, school_id)
    source_records = [
        get_record_usage(conn, record["record_code"])
        for record in list_records(conn, source_school_id=school_id)
    ]
    return {
        "school": school,
        "status_labels_zh": STATUS_LABELS_ZH,
        "issued_certificate_counts": issued["counts"],
        "issued_certificates": issued["certificates"],
        "source_records": source_records,
    }


def revoke_record(
    conn: sqlite3.Connection,
    record_code: str,
    source_school_id: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    with write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM credit_record WHERE record_code = ?", (record_code,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, "record_not_found", f"record {record_code} was not found")
        if row["source_school_id"] != source_school_id:
            raise ServiceError(
                403,
                "not_source_school",
                "only the publishing source school may revoke this record",
            )

        already_revoked = row["revoked_at"] is not None
        if not already_revoked:
            ts = now_iso()
            conn.execute(
                """
                UPDATE credit_record
                SET revoked_at = ?, revocation_reason = ?, revocation_school_id = ?
                WHERE record_code = ?
                """,
                (ts, reason, source_school_id, record_code),
            )

            active_holders = conn.execute(
                """
                SELECT DISTINCT c.certificate_code
                FROM certificate c
                JOIN certificate_item_record i
                  ON i.certificate_code = c.certificate_code
                WHERE i.record_code = ?
                  AND c.status = ?
                """,
                (record_code, STATUS_ACTIVE),
            ).fetchall()
            newly_pending = [r["certificate_code"] for r in active_holders]

            if newly_pending:
                ts_pending = now_iso()
                placeholders = ",".join("?" for _ in newly_pending)
                conn.execute(
                    f"""
                    UPDATE certificate
                    SET status = ?, pending_since = ?
                    WHERE status = ?
                      AND certificate_code IN ({placeholders})
                    """,
                    (STATUS_PENDING, ts_pending, STATUS_ACTIVE, *newly_pending),
                )
        else:
            newly_pending = []

        related_rows = conn.execute(
            """
            SELECT DISTINCT c.certificate_code, c.status
            FROM certificate c
            JOIN certificate_item_record i ON i.certificate_code = c.certificate_code
            WHERE i.record_code = ?
            ORDER BY c.certificate_code
            """,
            (record_code,),
        ).fetchall()
        related = [dict(r) for r in related_rows]
        record = get_record(conn, record_code)

    return {
        "record": record,
        "already_revoked": already_revoked,
        "newly_pending_certificate_codes": newly_pending,
        "related_certificates": related,
        "rule": "affected active certificates become revocation_pending immediately; their other records stay held until issuer confirmation.",
    }


def confirm_certificate_revocation(
    conn: sqlite3.Connection,
    certificate_code: str,
    issuer_school_id: str,
) -> dict[str, Any]:
    with write_transaction(conn):
        cert_row = conn.execute(
            "SELECT * FROM certificate WHERE certificate_code = ?",
            (certificate_code,),
        ).fetchone()
        if cert_row is None:
            raise ServiceError(
                404, "certificate_not_found", f"certificate {certificate_code} was not found"
            )
        if cert_row["issuer_school_id"] != issuer_school_id:
            raise ServiceError(
                403,
                "not_issuing_school",
                "only the issuing school may confirm this certificate's revocation",
            )

        if cert_row["status"] == STATUS_PROCESSED:
            return get_certificate_detail(conn, certificate_code)

        if cert_row["status"] == STATUS_ACTIVE:
            raise ServiceError(
                409,
                "no_pending_revocation",
                "certificate is active and has no source revocation awaiting confirmation",
            )

        revoked_basis = conn.execute(
            """
            SELECT i.record_code
            FROM certificate_item_record i
            JOIN credit_record r ON r.record_code = i.record_code
            WHERE i.certificate_code = ?
              AND r.revoked_at IS NOT NULL
            """,
            (certificate_code,),
        ).fetchall()
        if not revoked_basis:
            raise ServiceError(
                409,
                "pending_without_revoked_basis",
                "refusing to process pending certificate without a revoked source record",
            )

        ts = now_iso()
        conn.execute(
            """
            UPDATE certificate
            SET status = ?, processed_at = ?, confirmed_by_school_id = ?
            WHERE certificate_code = ?
            """,
            (STATUS_PROCESSED, ts, issuer_school_id, certificate_code),
        )
        conn.execute(
            """
            UPDATE certificate_item_record
            SET released_at = ?
            WHERE certificate_code = ?
              AND released_at IS NULL
            """,
            (ts, certificate_code),
        )
        result = get_certificate_detail(conn, certificate_code)

    return result
