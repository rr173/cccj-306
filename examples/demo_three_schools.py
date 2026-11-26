#!/usr/bin/env python3
"""Runnable three-school ledger walkthrough.

It demonstrates:
1. one MATH record qualifying for two requirements but assignable to only one;
2. occupancy by an active certificate;
3. immediate invalidity after source revocation;
4. continued occupancy during revocation_pending;
5. release and renewed issuance opportunity after issuer confirmation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, reset_db_file  # noqa: E402
from app.models import IssueIn  # noqa: E402
from app.seed import (  # noqa: E402
    DEF_LINHAI_BIZ_DATA,
    DEF_YUNSONG_DIGITAL,
    RECORD_CS_REVOKED,
    RECORD_VERSATILE,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
    STUDENT_LIN,
    seed_base_data,
)
from app.services import (  # noqa: E402
    ServiceError,
    confirm_certificate_revocation,
    get_certificate_detail,
    issue_certificate,
    review_definition,
    revoke_record,
    verify_certificate,
)


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def expect_conflict(label: str, conn, payload: IssueIn) -> None:
    try:
        issue_certificate(conn, payload)
    except ServiceError as exc:
        show(label, {"http_status": exc.status_code, "code": exc.code, "message": exc.message})
        assert exc.status_code == 409
    else:
        raise AssertionError(f"{label}: expected issuance conflict")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="three_schools_demo.db")
    args = parser.parse_args()

    reset_db_file(args.db)
    conn = connect(args.db)
    try:
        seed_base_data(conn)

        only_versatile = review_definition(
            conn, DEF_YUNSONG_DIGITAL, STUDENT_LIN, [RECORD_VERSATILE]
        )
        show(
            "1) Only the versatile record is reviewed: each item is eligible, "
            "but one whole record cannot cover both",
            {
                "feasible": only_versatile["feasible"],
                "conclusion": only_versatile["conclusion"],
                "candidate": {
                    "record_code": RECORD_VERSATILE,
                    "status": only_versatile["candidates"][0]["status"],
                    "eligible_requirement_codes": only_versatile["candidates"][0][
                        "eligible_requirement_codes"
                    ],
                },
            },
        )
        assert only_versatile["feasible"] is False

        yunsong_review = review_definition(
            conn,
            DEF_YUNSONG_DIGITAL,
            STUDENT_LIN,
            [RECORD_VERSATILE, RECORD_CS_REVOKED],
        )
        show(
            "2) Add CS-3: a valid distinct-record plan exists (review does not occupy)",
            {
                "feasible": yunsong_review["feasible"],
                "assignments": yunsong_review["assignments"],
            },
        )
        assert yunsong_review["feasible"] is True

        linhai_before = review_definition(
            conn, DEF_LINHAI_BIZ_DATA, STUDENT_LIN, [RECORD_VERSATILE]
        )
        show(
            "3) Before Yunsong issuance, Linhai can use the MATH-4 record",
            {"feasible": linhai_before["feasible"], "assignments": linhai_before["assignments"]},
        )
        assert linhai_before["feasible"] is True

        yunsong_cert = issue_certificate(
            conn,
            IssueIn(
                certificate_code="CERT-YUNSONG-001",
                definition_id=DEF_YUNSONG_DIGITAL,
                student_id=STUDENT_LIN,
                record_codes=[RECORD_VERSATILE, RECORD_CS_REVOKED],
            ),
        )
        show(
            "4) Yunsong issues atomically and writes one record per requirement",
            {
                "certificate_code": yunsong_cert["certificate_code"],
                "lifecycle_status": yunsong_cert["lifecycle_status"],
                "currently_valid": yunsong_cert["currently_valid"],
                "items": [
                    {
                        "requirement_code": i["requirement_code"],
                        "record_code": i["record_code"],
                    }
                    for i in yunsong_cert["items"]
                ],
            },
        )
        assert yunsong_cert["currently_valid"] is True
        assigned = {i["requirement_code"]: i["record_code"] for i in yunsong_cert["items"]}
        assert len(set(assigned.values())) == 2

        linhai_blocked_active = review_definition(
            conn, DEF_LINHAI_BIZ_DATA, STUDENT_LIN, [RECORD_VERSATILE]
        )
        show(
            "5) While Yunsong is active, Linhai sees the record as occupied",
            {
                "feasible": linhai_blocked_active["feasible"],
                "candidate_status": linhai_blocked_active["candidates"][0]["status"],
                "occupancy": linhai_blocked_active["candidates"][0]["occupancy"],
            },
        )
        assert linhai_blocked_active["feasible"] is False
        assert linhai_blocked_active["candidates"][0]["status"] == "occupied"
        expect_conflict(
            "5b) Linhai issuance is rejected by atomic recheck",
            conn,
            IssueIn(
                certificate_code="CERT-LINHAI-SHOULD-FAIL",
                definition_id=DEF_LINHAI_BIZ_DATA,
                student_id=STUDENT_LIN,
                record_codes=[RECORD_VERSATILE],
            ),
        )

        revocation = revoke_record(
            conn,
            RECORD_CS_REVOKED,
            SCHOOL_XINGHE,
            reason="来源学校复核发现原录入凭据有误，撤销该记录",
        )
        yunsong_after_revoke = get_certificate_detail(conn, "CERT-YUNSONG-001")
        verification_after_revoke = verify_certificate(conn, "CERT-YUNSONG-001")
        show(
            "6) Source school revokes CS-3: Yunsong certificate is immediately invalid/pending",
            {
                "revocation_result": {
                    "newly_pending_certificate_codes": revocation[
                        "newly_pending_certificate_codes"
                    ]
                },
                "certificate_status": yunsong_after_revoke["lifecycle_status"],
                "currently_valid": yunsong_after_revoke["currently_valid"],
                "external_verification": verification_after_revoke,
            },
        )
        assert yunsong_after_revoke["lifecycle_status"] == "revocation_pending"
        assert verification_after_revoke["valid"] is False

        linhai_pending = review_definition(
            conn, DEF_LINHAI_BIZ_DATA, STUDENT_LIN, [RECORD_VERSATILE]
        )
        show(
            "7) Before Yunsong confirms, the still-normal MATH-4 record remains held",
            {
                "feasible": linhai_pending["feasible"],
                "candidate_status": linhai_pending["candidates"][0]["status"],
                "occupancy": linhai_pending["candidates"][0]["occupancy"],
            },
        )
        assert linhai_pending["feasible"] is False
        assert (
            linhai_pending["candidates"][0]["occupancy"]["certificate_status"]
            == "revocation_pending"
        )
        expect_conflict(
            "7b) Linhai still cannot use MATH-4 during pending revocation",
            conn,
            IssueIn(
                certificate_code="CERT-LINHAI-SHOULD-FAIL-2",
                definition_id=DEF_LINHAI_BIZ_DATA,
                student_id=STUDENT_LIN,
                record_codes=[RECORD_VERSATILE],
            ),
        )

        processed = confirm_certificate_revocation(
            conn, "CERT-YUNSONG-001", SCHOOL_YUNSONG
        )
        show(
            "8) Yunsong confirms: certificate is processed and all item holds are released",
            {
                "lifecycle_status": processed["lifecycle_status"],
                "currently_valid": processed["currently_valid"],
                "items": [
                    {
                        "requirement_code": i["requirement_code"],
                        "record_code": i["record_code"],
                        "released_at": i["released_at"],
                        "still_occupies_record": i["still_occupies_record"],
                    }
                    for i in processed["items"]
                ],
            },
        )
        assert processed["lifecycle_status"] == "revocation_processed"
        assert all(i["released_at"] is not None for i in processed["items"])

        linhai_review_after = review_definition(
            conn, DEF_LINHAI_BIZ_DATA, STUDENT_LIN, [RECORD_VERSATILE]
        )
        linhai_cert = issue_certificate(
            conn,
            IssueIn(
                certificate_code="CERT-LINHAI-001",
                definition_id=DEF_LINHAI_BIZ_DATA,
                student_id=STUDENT_LIN,
                record_codes=[RECORD_VERSATILE],
            ),
        )
        show(
            "9) After confirmation, Linhai has a new issuance opportunity and issues",
            {
                "review_feasible": linhai_review_after["feasible"],
                "candidate_status": linhai_review_after["candidates"][0]["status"],
                "linhai_certificate": {
                    "certificate_code": linhai_cert["certificate_code"],
                    "lifecycle_status": linhai_cert["lifecycle_status"],
                    "currently_valid": linhai_cert["currently_valid"],
                    "items": [
                        {
                            "requirement_code": i["requirement_code"],
                            "record_code": i["record_code"],
                        }
                        for i in linhai_cert["items"]
                    ],
                },
            },
        )
        assert linhai_review_after["feasible"] is True
        assert linhai_cert["currently_valid"] is True

        show(
            "10) Original Yunsong basis remains queryable but never verifies as valid",
            {
                "yunsong_detail": get_certificate_detail(conn, "CERT-YUNSONG-001"),
                "yunsong_verification": verify_certificate(conn, "CERT-YUNSONG-001"),
                "linhai_verification": verify_certificate(conn, "CERT-LINHAI-001"),
            },
        )
        assert verify_certificate(conn, "CERT-YUNSONG-001")["valid"] is False
        assert verify_certificate(conn, "CERT-LINHAI-001")["valid"] is True

        print("\nDEMO OK: all three-school invariants passed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
