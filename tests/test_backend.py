from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.models import IssueIn
from app.seed import (
    DEF_LINHAI_BIZ_DATA,
    DEF_YUNSONG_DIGITAL,
    RECORD_CS_REVOKED,
    RECORD_VERSATILE,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
    STUDENT_LIN,
    seed_base_data,
)
from app.services import (
    ServiceError,
    create_record,
    issue_certificate,
    revoke_record,
    verify_certificate,
)


class ApiFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "test.db")
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        conn = connect(self.db_path)
        try:
            seed_base_data(conn)
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.client.close()
        self._tmp.cleanup()

    def test_review_failure_reports_dual_eligibility_but_single_use_limit(self):
        resp = self.client.post(
            "/api/reviews",
            json={
                "definition_id": DEF_YUNSONG_DIGITAL,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["feasible"])
        candidate = body["candidates"][0]
        self.assertEqual(candidate["status"], "available")
        self.assertEqual(
            candidate["eligible_requirement_codes"], ["logic", "quant"]
        )
        self.assertEqual(body["assignments"], [])

    def test_full_revocation_hold_and_release_flow_over_http(self):
        # Review with two records succeeds but does not occupy.
        review = self.client.post(
            "/api/reviews",
            json={
                "definition_id": DEF_YUNSONG_DIGITAL,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE, RECORD_CS_REVOKED],
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertTrue(review.json()["feasible"])

        # Linhai can review positively before the record is occupied.
        before = self.client.post(
            "/api/reviews",
            json={
                "definition_id": DEF_LINHAI_BIZ_DATA,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        ).json()
        self.assertTrue(before["feasible"])

        issue = self.client.post(
            "/api/certificates/issue",
            json={
                "certificate_code": "CERT-YUN-1",
                "definition_id": DEF_YUNSONG_DIGITAL,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE, RECORD_CS_REVOKED],
            },
        )
        self.assertEqual(issue.status_code, 201, issue.text)
        cert = issue.json()
        self.assertEqual(cert["lifecycle_status"], "active")
        used_records = [item["record_code"] for item in cert["items"]]
        self.assertEqual(len(used_records), len(set(used_records)))
        self.assertIn(RECORD_VERSATILE, used_records)
        self.assertIn(RECORD_CS_REVOKED, used_records)

        # A second issuer cannot use the occupied versatile record.
        blocked = self.client.post(
            "/api/certificates/issue",
            json={
                "certificate_code": "CERT-LIN-BLOCKED",
                "definition_id": DEF_LINHAI_BIZ_DATA,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        )
        self.assertEqual(blocked.status_code, 409)
        error = blocked.json()["error"]
        self.assertEqual(error["code"], "no_feasible_assignment")
        candidate = error["details"]["review"]["candidates"][0]
        self.assertEqual(candidate["status"], "occupied")
        self.assertEqual(candidate["occupancy"]["certificate_code"], "CERT-YUN-1")

        # Only the source school may publish revocation.
        forbidden = self.client.post(
            f"/api/records/{RECORD_CS_REVOKED}/revoke",
            json={"source_school_id": SCHOOL_YUNSONG, "reason": "bad actor"},
        )
        self.assertEqual(forbidden.status_code, 403)

        revoked = self.client.post(
            f"/api/records/{RECORD_CS_REVOKED}/revoke",
            json={"source_school_id": SCHOOL_XINGHE, "reason": "invalid source evidence"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(
            revoked.json()["newly_pending_certificate_codes"], ["CERT-YUN-1"]
        )

        verification = self.client.get("/verify/CERT-YUN-1")
        self.assertEqual(verification.status_code, 200)
        self.assertFalse(verification.json()["valid"])
        self.assertEqual(verification.json()["status"], "revocation_pending")

        school_view = self.client.get(
            f"/api/schools/{SCHOOL_YUNSONG}/certificates",
            params={"status": "revocation_pending"},
        ).json()
        self.assertEqual(school_view["counts"]["revocation_pending"], 1)
        self.assertEqual(
            school_view["certificates"][0]["lifecycle_status"],
            "revocation_pending",
        )

        # Pending certificate still holds the normal MATH record.
        pending_review = self.client.post(
            "/api/reviews",
            json={
                "definition_id": DEF_LINHAI_BIZ_DATA,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        ).json()
        self.assertFalse(pending_review["feasible"])
        occupancy = pending_review["candidates"][0]["occupancy"]
        self.assertEqual(occupancy["certificate_status"], "revocation_pending")

        pending_issue = self.client.post(
            "/api/certificates/issue",
            json={
                "certificate_code": "CERT-LIN-PENDING-BLOCKED",
                "definition_id": DEF_LINHAI_BIZ_DATA,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        )
        self.assertEqual(pending_issue.status_code, 409)

        # Wrong issuer cannot confirm.
        wrong_confirm = self.client.post(
            "/api/certificates/CERT-YUN-1/confirm-revocation",
            json={"issuer_school_id": SCHOOL_XINGHE},
        )
        self.assertEqual(wrong_confirm.status_code, 403)

        confirmed = self.client.post(
            "/api/certificates/CERT-YUN-1/confirm-revocation",
            json={"issuer_school_id": SCHOOL_YUNSONG},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        confirmed_body = confirmed.json()
        self.assertEqual(
            confirmed_body["lifecycle_status"], "revocation_processed"
        )
        self.assertTrue(all(i["released_at"] for i in confirmed_body["items"]))
        self.assertEqual(len(confirmed_body["items"]), 2)

        # Original basis remains queryable.
        detail = self.client.get("/api/certificates/CERT-YUN-1").json()
        self.assertEqual(len(detail["items"]), 2)
        self.assertIn(RECORD_CS_REVOKED, detail["revoked_basis_record_codes"])

        # The normal record is released and Linhai can now issue.
        linhai_issue = self.client.post(
            "/api/certificates/issue",
            json={
                "certificate_code": "CERT-LIN-1",
                "definition_id": DEF_LINHAI_BIZ_DATA,
                "student_id": STUDENT_LIN,
                "record_codes": [RECORD_VERSATILE],
            },
        )
        self.assertEqual(linhai_issue.status_code, 201, linhai_issue.text)
        self.assertTrue(linhai_issue.json()["currently_valid"])
        self.assertTrue(self.client.get("/verify/CERT-LIN-1").json()["valid"])
        self.assertFalse(self.client.get("/verify/CERT-YUN-1").json()["valid"])

    def test_sum_points_mode_can_use_several_records_for_one_item(self):
        definition = self.client.post(
            "/api/credential-definitions",
            json={
                "definition_id": "def_linhai_sum",
                "issuer_school_id": "sch_linhai",
                "title": "累计分值规则测试证书",
                "requirements": [
                    {
                        "requirement_code": "elective_total",
                        "accepted_categories": ["MATH", "STAT"],
                        "min_points": 5,
                        "points_mode": "sum_points",
                    }
                ],
            },
        )
        self.assertEqual(definition.status_code, 201, definition.text)

        for code, category, points in [
            ("SUM-1", "MATH", 2),
            ("SUM-2", "STAT", 3),
        ]:
            created = self.client.post(
                "/api/records",
                json={
                    "record_code": code,
                    "source_school_id": SCHOOL_XINGHE,
                    "student_id": STUDENT_LIN,
                    "category": category,
                    "points": points,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)

        review = self.client.post(
            "/api/reviews",
            json={
                "definition_id": "def_linhai_sum",
                "student_id": STUDENT_LIN,
                "record_codes": ["SUM-1", "SUM-2"],
            },
        ).json()
        self.assertTrue(review["feasible"], review)
        group = review["assignments"][0]
        self.assertCountEqual(group["record_codes"], ["SUM-1", "SUM-2"])
        self.assertEqual(group["assigned_points"], 5)

        issue = self.client.post(
            "/api/certificates/issue",
            json={
                "certificate_code": "CERT-SUM-1",
                "definition_id": "def_linhai_sum",
                "student_id": STUDENT_LIN,
                "record_codes": ["SUM-1", "SUM-2"],
            },
        )
        self.assertEqual(issue.status_code, 201, issue.text)
        body = issue.json()
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["requirement_results"][0]["assigned_points"], 5)

    def test_published_record_values_are_immutable(self):
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE credit_record SET points = 99 WHERE record_code = ?",
                    (RECORD_VERSATILE,),
                )
        finally:
            conn.close()


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "concurrency.db")
        # create_app initializes schema; no FastAPI client needed for service race.
        create_app(self.db_path)
        conn = connect(self.db_path)
        try:
            seed_base_data(conn)
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_one_concurrent_issuance_can_hold_same_record(self):
        workers = 12
        barrier = threading.Barrier(workers)
        outcomes: list[tuple[str, str]] = []
        outcomes_lock = threading.Lock()

        def issue(index: int):
            conn = connect(self.db_path)
            try:
                payload = IssueIn(
                    certificate_code=f"CERT-RACE-{index:02d}",
                    definition_id=DEF_LINHAI_BIZ_DATA,
                    student_id=STUDENT_LIN,
                    record_codes=[RECORD_VERSATILE],
                )
                barrier.wait(timeout=10)
                cert = issue_certificate(conn, payload)
                with outcomes_lock:
                    outcomes.append(("ok", cert["certificate_code"]))
            except ServiceError as exc:
                with outcomes_lock:
                    outcomes.append(("conflict", exc.code))
            except Exception as exc:  # pragma: no cover - surfaced in assertion
                with outcomes_lock:
                    outcomes.append(("error", repr(exc)))
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(issue, range(workers)))

        oks = [item for item in outcomes if item[0] == "ok"]
        conflicts = [item for item in outcomes if item[0] == "conflict"]
        errors = [item for item in outcomes if item[0] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(len(conflicts), workers - 1, outcomes)

        conn = connect(self.db_path)
        try:
            active_holds = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM certificate_item_record
                WHERE record_code = ? AND released_at IS NULL
                """,
                (RECORD_VERSATILE,),
            ).fetchone()["count"]
            self.assertEqual(active_holds, 1)
        finally:
            conn.close()
    def test_revoke_and_issue_race_never_leaves_revoked_basis_valid(self):
        iterations = 20
        for index in range(iterations):
            record_code = f"RACE-REV-REC-{index}"
            certificate_code = f"CERT-RACE-REV-{index}"
            setup = connect(self.db_path)
            try:
                create_record(
                    setup,
                    record_code=record_code,
                    source_school_id=SCHOOL_XINGHE,
                    student_id=STUDENT_LIN,
                    category="MATH",
                    points=4,
                )
            finally:
                setup.close()

            barrier = threading.Barrier(2)
            outcomes: dict[str, str] = {}
            outcomes_lock = threading.Lock()

            def issue_worker():
                conn = connect(self.db_path)
                try:
                    barrier.wait(timeout=10)
                    issue_certificate(
                        conn,
                        IssueIn(
                            certificate_code=certificate_code,
                            definition_id=DEF_LINHAI_BIZ_DATA,
                            student_id=STUDENT_LIN,
                            record_codes=[record_code],
                        ),
                    )
                    with outcomes_lock:
                        outcomes["issue"] = "ok"
                except ServiceError:
                    with outcomes_lock:
                        outcomes["issue"] = "conflict"
                except Exception as exc:  # pragma: no cover
                    with outcomes_lock:
                        outcomes["issue"] = f"error:{exc!r}"
                finally:
                    conn.close()

            def revoke_worker():
                conn = connect(self.db_path)
                try:
                    barrier.wait(timeout=10)
                    revoke_record(conn, record_code, SCHOOL_XINGHE, "race revocation")
                    with outcomes_lock:
                        outcomes["revoke"] = "ok"
                except Exception as exc:  # pragma: no cover
                    with outcomes_lock:
                        outcomes["revoke"] = f"error:{exc!r}"
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda fn: fn(), [issue_worker, revoke_worker]))

            self.assertNotIn("error", outcomes.get("issue", ""), outcomes)
            self.assertEqual(outcomes.get("revoke"), "ok", outcomes)

            conn = connect(self.db_path)
            try:
                cert_row = conn.execute(
                    "SELECT status FROM certificate WHERE certificate_code = ?",
                    (certificate_code,),
                ).fetchone()
                if outcomes["issue"] == "ok":
                    self.assertIsNotNone(cert_row, outcomes)
                    self.assertEqual(cert_row["status"], "revocation_pending", outcomes)
                    verification = verify_certificate(conn, certificate_code)
                    self.assertFalse(verification["valid"])
                    self.assertIn(record_code, verification["revoked_basis_record_codes"])
                    holds = conn.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM certificate_item_record
                        WHERE record_code = ? AND released_at IS NULL
                        """,
                        (record_code,),
                    ).fetchone()["count"]
                    self.assertEqual(holds, 1)
                else:
                    self.assertIsNone(cert_row, outcomes)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
