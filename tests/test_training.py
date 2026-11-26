from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.models import IssueIn
from app.seed import (
    DEF_LINHAI_BIZ_DATA,
    DEF_YUNSONG_DIGITAL,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
)
from app.services import (
    ServiceError,
    create_record,
    create_student,
    issue_certificate,
)
from app import training

TERM = "term_train_1"
CLASS_A = "cls_a"
CLASS_B = "cls_b"
CLASS_C = "cls_c"


def _deadline(hours: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class TrainingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "training.db")
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        conn = connect(self.db_path)
        try:
            self._seed(conn)
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.client.close()
        self._tmp.cleanup()

    def _seed(self, conn) -> None:
        # Students stu1..stu4, each with one MATH-4 record and an active
        # Linhai certificate built on it.
        from app.seed import seed_base_data

        seed_base_data(conn)
        self.students = ["stu1", "stu2", "stu3", "stu4"]
        for index, student_id in enumerate(self.students, start=1):
            create_student(conn, student_id, f"学生{index}")
            record_code = f"TR-REC-{index}"
            create_record(
                conn,
                record_code,
                SCHOOL_XINGHE,
                student_id,
                "MATH",
                4,
            )
            issue_certificate(
                conn,
                IssueIn(
                    certificate_code=f"CERT-TR-{index}",
                    definition_id=DEF_LINHAI_BIZ_DATA,
                    student_id=student_id,
                    record_codes=[record_code],
                ),
            )

        term = self.client.post(
            "/api/training/terms",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "title": "进阶实训第一期",
                "accepted_definition_ids": [DEF_LINHAI_BIZ_DATA, DEF_YUNSONG_DIGITAL],
            },
        )
        self.assertEqual(term.status_code, 201, term.text)
        for class_id, name, capacity in [
            (CLASS_A, "甲班", 2),
            (CLASS_B, "乙班", 2),
            (CLASS_C, "丙班", 1),
        ]:
            resp = self.client.post(
                "/api/training/classes",
                json={
                    "class_id": class_id,
                    "term_id": TERM,
                    "name": name,
                    "capacity": capacity,
                },
            )
            self.assertEqual(resp.status_code, 201, resp.text)

    def _enroll(self, student_id: str, class_id: str, cert_index: int) -> dict:
        resp = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": student_id,
                "class_id": class_id,
                "certificate_code": f"CERT-TR-{cert_index}",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _capacity(self, class_id: str) -> dict:
        resp = self.client.get(f"/api/training/terms/{TERM}/classes")
        self.assertEqual(resp.status_code, 200, resp.text)
        for item in resp.json()["classes"]:
            if item["class_id"] == class_id:
                return item
        raise KeyError(class_id)


class EnrollmentRuleTest(TrainingTestBase):
    def test_enrollment_validation_and_one_seat_per_student(self):
        self._enroll("stu1", CLASS_A, 1)

        # Re-enrolling the same student in the same term is rejected.
        duplicate = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu1",
                "class_id": CLASS_B,
                "certificate_code": "CERT-TR-1",
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "already_enrolled")

        # A student cannot use someone else's certificate.
        wrong = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu2",
                "class_id": CLASS_A,
                "certificate_code": "CERT-TR-1",
            },
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json()["error"]["code"], "certificate_wrong_student")

        # Certificate of a non-accepted definition is rejected.
        other = self.client.post(
            "/api/training/terms",
            json={
                "term_id": "term_strict",
                "organizer_school_id": SCHOOL_LINHAI,
                "title": "只收云杉证书的期",
                "accepted_definition_ids": [DEF_YUNSONG_DIGITAL],
            },
        )
        self.assertEqual(other.status_code, 201, other.text)
        strict_class = self.client.post(
            "/api/training/classes",
            json={
                "class_id": "cls_strict",
                "term_id": "term_strict",
                "name": "严格班",
                "capacity": 5,
            },
        )
        self.assertEqual(strict_class.status_code, 201, strict_class.text)
        not_accepted = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": "term_strict",
                "student_id": "stu2",
                "class_id": "cls_strict",
                "certificate_code": "CERT-TR-2",
            },
        )
        self.assertEqual(not_accepted.status_code, 409)
        self.assertEqual(not_accepted.json()["error"]["code"], "credential_type_not_accepted")

        # Withdraw frees the seat and re-enrollment is allowed.
        withdrawn = self.client.post(
            "/api/training/withdrawals",
            json={"term_id": TERM, "student_id": "stu1"},
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        self.assertEqual(withdrawn.json()["status"], "withdrawn")
        again = self._enroll("stu1", CLASS_B, 1)
        self.assertEqual(again["class_id"], CLASS_B)

        view = self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()
        self.assertEqual(view["formal_class_id"], CLASS_B)
        self.assertEqual(len(view["enrollment_history"]), 2)

    def test_full_class_rejects_new_enrollment_and_revoked_certificate_releases_seat(self):
        # Class C has capacity 1.
        self._enroll("stu1", CLASS_C, 1)
        full = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu2",
                "class_id": CLASS_C,
                "certificate_code": "CERT-TR-2",
            },
        )
        self.assertEqual(full.status_code, 409)
        self.assertEqual(full.json()["error"]["code"], "class_full")

        # Revoking the basis record invalidates the certificate and the seat
        # immediately, without any issuer confirmation step.
        revoke = self.client.post(
            "/api/records/TR-REC-1/revoke",
            json={"source_school_id": SCHOOL_XINGHE, "reason": "复核撤销"},
        )
        self.assertEqual(revoke.status_code, 200, revoke.text)
        effect = revoke.json()["training_effect"]
        self.assertEqual(effect["disqualified_enrollment_ids"], [1])

        roster = self.client.get(
            f"/api/training/terms/{TERM}/classes/{CLASS_C}/roster",
            params={"organizer_school_id": SCHOOL_LINHAI},
        ).json()
        self.assertEqual(roster["roster"], [])
        self.assertEqual(roster["capacity_view"]["available"], 1)

        # stu2 can now take the freed seat.
        self._enroll("stu2", CLASS_C, 2)

        # The disqualified student cannot keep using the dead certificate,
        # and the system does not silently swap in another certificate.
        view = self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()
        self.assertIsNone(view["formal_class_id"])
        self.assertEqual(view["enrollment_history"][0]["status"], "disqualified")
        reuse_dead = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu1",
                "class_id": CLASS_A,
                "certificate_code": "CERT-TR-1",
            },
        )
        self.assertEqual(reuse_dead.status_code, 409)
        self.assertEqual(reuse_dead.json()["error"]["code"], "certificate_not_valid")


class SwapOrderRuleTest(TrainingTestBase):
    def test_swap_order_creation_rules(self):
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_B, 2)

        # Target same as current class.
        bad = self.client.post(
            "/api/training/swap-orders",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(bad.json()["error"]["code"], "target_same_as_current")

        # Non-enrolled student.
        bad = self.client.post(
            "/api/training/swap-orders",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu3", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(bad.json()["error"]["code"], "participant_not_enrolled")

        # Duplicate student within one payload is a 422 schema error.
        bad = self.client.post(
            "/api/training/swap-orders",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                    {"student_id": "stu1", "to_class_id": CLASS_C},
                ],
            },
        )
        self.assertEqual(bad.status_code, 422)

        # Only the organizer may submit orders.
        bad = self.client.post(
            "/api/training/swap-orders",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_YUNSONG,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                ],
            },
        )
        self.assertEqual(bad.status_code, 403)

        # A student may not belong to two open orders.
        first = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-1",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                ],
            },
        )
        self.assertEqual(first.status_code, 201, first.text)
        second = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-2",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_C},
                ],
            },
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "student_already_in_pending_swap")

    def test_pending_reservation_blocks_other_orders_and_new_enrollments(self):
        # A full (capacity 1) class C.
        self._enroll("stu3", CLASS_C, 3)
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_B, 2)

        # stu1 wants into C while nobody in C moves out in the same order:
        # net +1 for C with zero available seats -> rejected as a whole.
        blocked = self.client.post(
            "/api/training/swap-orders",
            json={
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_C},
                ],
            },
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "swap_capacity_exceeded")

        # A two-person order stu2 -> A (A: 2 cap, currently 1 enrolled)
        # reserves the last seat of A and one incoming seat of B.
        order = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-HOLD",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu2", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(order.status_code, 201, order.text)
        self.assertEqual(self._capacity(CLASS_A)["available"], 0)
        self.assertEqual(self._capacity(CLASS_A)["held_pending_in"], 1)

        # New enrollment cannot consume the held seat.
        full = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu4",
                "class_id": CLASS_A,
                "certificate_code": "CERT-TR-4",
            },
        )
        self.assertEqual(full.status_code, 409)
        self.assertEqual(full.json()["error"]["code"], "class_full")

        # Roster still shows the original class; no change before all confirm.
        view = self.client.get(f"/api/training/terms/{TERM}/students/stu2").json()
        self.assertEqual(view["formal_class_id"], CLASS_B)

    def test_seats_freed_by_one_order_are_not_available_to_another_order(self):
        # Class A is full (cap 2); class B still has a free seat.
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_A, 2)
        self._enroll("stu3", CLASS_B, 3)
        # Order 1: stu1 vacates an A seat and moves to B; stu3 moves into A.
        # As a whole it balances: A and B both have net change 0.
        order1 = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-X",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                    {"student_id": "stu3", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(order1.status_code, 201, order1.text)

        # Order 2 tries to treat the A seat that order 1 is vacating as an
        # open seat and moves another student into A without giving A a seat
        # back. It must fail: the freed seat belongs to order 1's own move.
        self._enroll("stu4", CLASS_B, 4)
        order2 = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-Y",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu4", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(order2.status_code, 409)
        self.assertEqual(order2.json()["error"]["code"], "swap_capacity_exceeded")


class FullSwapFlowTest(TrainingTestBase):
    def test_full_classes_swap_atomically_after_all_confirmations(self):
        # Fill both capacity-2 classes.
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_A, 2)
        self._enroll("stu3", CLASS_B, 3)
        self._enroll("stu4", CLASS_B, 4)
        self.assertEqual(self._capacity(CLASS_A)["available"], 0)
        self.assertEqual(self._capacity(CLASS_B)["available"], 0)

        order = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-SWAP",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                    {"student_id": "stu3", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(order.status_code, 201, order.text)
        body = order.json()
        self.assertEqual(body["net_class_increases"][CLASS_A], 0)
        self.assertEqual(body["net_class_increases"][CLASS_B], 0)

        # Outsiders still cannot grab seats: both classes remain at zero
        # available because the order holds both cross-seats.
        # (Create another student for the attempt.)
        conn = connect(self.db_path)
        try:
            create_student(conn, "stu5", "学生5")
            create_record(conn, "TR-REC-5", SCHOOL_XINGHE, "stu5", "MATH", 4)
            issue_certificate(
                conn,
                IssueIn(
                    certificate_code="CERT-TR-5",
                    definition_id=DEF_LINHAI_BIZ_DATA,
                    student_id="stu5",
                    record_codes=["TR-REC-5"],
                ),
            )
        finally:
            conn.close()
        attempt = self.client.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu5",
                "class_id": CLASS_A,
                "certificate_code": "CERT-TR-5",
            },
        )
        self.assertEqual(attempt.status_code, 409)

        # Only the participant themselves can respond.
        forbidden = self.client.post(
            "/api/training/swap-orders/SW-SWAP/responses",
            json={"student_id": "stu2", "response": "confirmed"},
        )
        self.assertEqual(forbidden.status_code, 403)

        first = self.client.post(
            "/api/training/swap-orders/SW-SWAP/responses",
            json={"student_id": "stu1", "response": "confirmed"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "pending_confirmation")

        # Roster is unchanged while waiting.
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()[
                "formal_class_id"
            ],
            CLASS_A,
        )

        # Late/duplicate response cannot change anything.
        duplicate = self.client.post(
            "/api/training/swap-orders/SW-SWAP/responses",
            json={"student_id": "stu1", "response": "rejected"},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error"]["code"], "already_responded")

        second = self.client.post(
            "/api/training/swap-orders/SW-SWAP/responses",
            json={"student_id": "stu3", "response": "confirmed"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["status"], "swapped")

        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()[
                "formal_class_id"
            ],
            CLASS_B,
        )
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu3").json()[
                "formal_class_id"
            ],
            CLASS_A,
        )
        # Non-participants never moved.
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu2").json()[
                "formal_class_id"
            ],
            CLASS_A,
        )
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu4").json()[
                "formal_class_id"
            ],
            CLASS_B,
        )
        # Capacities stay exactly full, never overbooked.
        self.assertEqual(self._capacity(CLASS_A)["enrolled"], 2)
        self.assertEqual(self._capacity(CLASS_B)["enrolled"], 2)
        self.assertEqual(self._capacity(CLASS_A)["available"], 0)
        self.assertEqual(self._capacity(CLASS_B)["available"], 0)

        # A response on a closed order is rejected, so a delayed confirmation
        # can never trigger another move.
        late = self.client.post(
            "/api/training/swap-orders/SW-SWAP/responses",
            json={"student_id": "stu1", "response": "confirmed"},
        )
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["error"]["code"], "swap_order_closed")

    def test_three_way_joint_swap_moves_everyone_together(self):
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_B, 2)
        self._enroll("stu3", CLASS_C, 3)  # capacity 1
        order = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-3",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                    {"student_id": "stu2", "to_class_id": CLASS_C},
                    {"student_id": "stu3", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(order.status_code, 201, order.text)
        for student_id in self.students[:3]:
            resp = self.client.post(
                "/api/training/swap-orders/SW-3/responses",
                json={"student_id": student_id, "response": "confirmed"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        final = self.client.get("/api/training/swap-orders/SW-3").json()
        self.assertEqual(final["status"], "swapped")
        expected = {"stu1": CLASS_B, "stu2": CLASS_C, "stu3": CLASS_A}
        for student_id, class_id in expected.items():
            self.assertEqual(
                self.client.get(
                    f"/api/training/terms/{TERM}/students/{student_id}"
                ).json()["formal_class_id"],
                class_id,
            )
        self.assertEqual(self._capacity(CLASS_C)["enrolled"], 1)


class OrderEndingTest(TrainingTestBase):
    def _make_pending_order(self, order_id: str = "SW-END") -> str:
        self._enroll("stu1", CLASS_A, 1)
        self._enroll("stu2", CLASS_B, 2)
        resp = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": order_id,
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                    {"student_id": "stu2", "to_class_id": CLASS_A},
                ],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return order_id

    def test_rejection_ends_order_releases_holds_and_keeps_original_classes(self):
        order_id = self._make_pending_order()
        # A balanced A<->B order holds one incoming and one outgoing seat per
        # class, so the net reservation is zero; the holds themselves remain
        # visible until the order ends.
        view_a = self._capacity(CLASS_A)
        self.assertEqual(view_a["held_pending_in"], 1)
        self.assertEqual(view_a["held_pending_out"], 1)
        self.assertEqual(view_a["net_pending_reservation"], 0)

        resp = self.client.post(
            f"/api/training/swap-orders/{order_id}/responses",
            json={"student_id": "stu2", "response": "rejected"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "rejected")

        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()[
                "formal_class_id"
            ],
            CLASS_A,
        )
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu2").json()[
                "formal_class_id"
            ],
            CLASS_B,
        )
        # Reservations released; no pending holds remain.
        view_a = self._capacity(CLASS_A)
        view_b = self._capacity(CLASS_B)
        self.assertEqual(view_a["held_pending_in"], 0)
        self.assertEqual(view_b["held_pending_in"], 0)
        self.assertEqual(view_a["net_pending_reservation"], 0)

        # The student can now join a fresh order.
        again = self.client.post(
            "/api/training/swap-orders",
            json={
                "order_id": "SW-AGAIN",
                "term_id": TERM,
                "organizer_school_id": SCHOOL_LINHAI,
                "confirm_deadline": _deadline(),
                "participants": [
                    {"student_id": "stu1", "to_class_id": CLASS_B},
                ],
            },
        )
        self.assertEqual(again.status_code, 201, again.text)

    def test_withdrawal_ends_related_pending_order(self):
        order_id = self._make_pending_order()
        withdrawn = self.client.post(
            "/api/training/withdrawals",
            json={"term_id": TERM, "student_id": "stu1"},
        )
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        order = self.client.get(f"/api/training/swap-orders/{order_id}").json()
        self.assertEqual(order["status"], "withdrawn")
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu2").json()[
                "formal_class_id"
            ],
            CLASS_B,
        )

    def test_certificate_revocation_mid_swap_disqualifies_and_ends_order(self):
        order_id = self._make_pending_order()
        # stu1 confirms first; then the basis of stu1's certificate is revoked.
        confirmed = self.client.post(
            f"/api/training/swap-orders/{order_id}/responses",
            json={"student_id": "stu1", "response": "confirmed"},
        )
        self.assertEqual(confirmed.status_code, 200)

        revoke = self.client.post(
            "/api/records/TR-REC-1/revoke",
            json={"source_school_id": SCHOOL_XINGHE, "reason": "来源撤销"},
        )
        self.assertEqual(revoke.status_code, 200, revoke.text)
        self.assertIn(order_id, revoke.json()["training_effect"]["ended_swap_order_ids"])

        order = self.client.get(f"/api/training/swap-orders/{order_id}").json()
        self.assertEqual(order["status"], "disqualified")

        # stu1's seat is released immediately; stu2's original class is intact.
        self.assertIsNone(
            self.client.get(f"/api/training/terms/{TERM}/students/stu1").json()[
                "formal_class_id"
            ]
        )
        self.assertEqual(
            self.client.get(f"/api/training/terms/{TERM}/students/stu2").json()[
                "formal_class_id"
            ],
            CLASS_B,
        )
        # A late confirmation cannot revive the order.
        late = self.client.post(
            f"/api/training/swap-orders/{order_id}/responses",
            json={"student_id": "stu2", "response": "confirmed"},
        )
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["error"]["code"], "swap_order_closed")

    def test_expired_order_is_swept_and_stops_blocking_seats_across_restart(self):
        conn = connect(self.db_path)
        try:
            self._enroll_via_service(conn, "stu1", CLASS_A, 1)
            self._enroll_via_service(conn, "stu2", CLASS_B, 2)
            # Insert an already-due order directly: the service layer refuses
            # deadlines in the past, but a restart must still cope with rows
            # whose deadline elapsed while the process was down.
            past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            ts = datetime.now(timezone.utc).isoformat()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO swap_order(
                    order_id, term_id, organizer_school_id, status,
                    confirm_deadline, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "SW-EXPIRED",
                    TERM,
                    SCHOOL_LINHAI,
                    training.ORDER_PENDING,
                    past,
                    ts,
                ),
            )
            conn.execute(
                """
                INSERT INTO swap_participant(
                    order_id, student_id, from_class_id, to_class_id, order_open
                ) VALUES (?, ?, ?, ?, 1)
                """,
                ("SW-EXPIRED", "stu1", CLASS_A, CLASS_B),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

        # The first read after the deadline sweeps the order and releases its
        # reservation, even though no background worker ran.
        classes = self.client.get(f"/api/training/terms/{TERM}/classes").json()["classes"]
        class_b = next(item for item in classes if item["class_id"] == CLASS_B)
        self.assertEqual(class_b["held_pending_in"], 0)
        self.assertEqual(class_b["available"], class_b["capacity"] - class_b["enrolled"])
        order = self.client.get("/api/training/swap-orders/SW-EXPIRED").json()
        self.assertEqual(order["status"], "expired")

        # A restart against the same database keeps the expired order closed:
        # it must never block seats again.
        self.client.close()
        restarted_app = create_app(self.db_path)
        restarted = TestClient(restarted_app)
        classes = restarted.get(f"/api/training/terms/{TERM}/classes").json()["classes"]
        class_b = next(item for item in classes if item["class_id"] == CLASS_B)
        self.assertEqual(class_b["held_pending_in"], 0)
        self.assertEqual(class_b["available"], class_b["capacity"] - class_b["enrolled"])
        order = restarted.get("/api/training/swap-orders/SW-EXPIRED").json()
        self.assertEqual(order["status"], "expired")

        # Freed seat can now be used by a new enrollment.
        conn = connect(self.db_path)
        try:
            create_student(conn, "stu9", "学生9")
            create_record(conn, "TR-REC-9", SCHOOL_XINGHE, "stu9", "MATH", 4)
            issue_certificate(
                conn,
                IssueIn(
                    certificate_code="CERT-TR-9",
                    definition_id=DEF_LINHAI_BIZ_DATA,
                    student_id="stu9",
                    record_codes=["TR-REC-9"],
                ),
            )
        finally:
            conn.close()
        enroll = restarted.post(
            "/api/training/enrollments",
            json={
                "term_id": TERM,
                "student_id": "stu9",
                "class_id": CLASS_B,
                "certificate_code": "CERT-TR-9",
            },
        )
        self.assertEqual(enroll.status_code, 201, enroll.text)
        restarted.close()

    def test_unexpired_order_survives_restart_and_can_still_be_confirmed(self):
        order_id = self._make_pending_order("SW-RESTART")
        self.client.post(
            f"/api/training/swap-orders/{order_id}/responses",
            json={"student_id": "stu1", "response": "confirmed"},
        )

        # Restart the service process against the same database file.
        self.client.close()
        restarted = TestClient(create_app(self.db_path))
        order = restarted.get(f"/api/training/swap-orders/{order_id}").json()
        self.assertEqual(order["status"], "pending_confirmation")
        participant = next(
            p for p in order["participants"] if p["student_id"] == "stu1"
        )
        self.assertEqual(participant["response"], "confirmed")

        resp = restarted.post(
            f"/api/training/swap-orders/{order_id}/responses",
            json={"student_id": "stu2", "response": "confirmed"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "swapped")
        self.assertEqual(
            restarted.get(f"/api/training/terms/{TERM}/students/stu2").json()[
                "formal_class_id"
            ],
            CLASS_A,
        )
        restarted.close()

    def _enroll_via_service(self, conn, student_id: str, class_id: str, index: int) -> None:
        from app.models import EnrollmentIn

        training.enroll(
            conn,
            EnrollmentIn(
                term_id=TERM,
                student_id=student_id,
                class_id=class_id,
                certificate_code=f"CERT-TR-{index}",
            ),
        )


class TrainingConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "race.db")
        create_app(self.db_path)
        conn = connect(self.db_path)
        try:
            from app.seed import seed_base_data

            seed_base_data(conn)
            from app.models import TrainingClassIn, TrainingTermIn

            create_student(conn, "stu_race", "竞赛学生")
            create_record(conn, "RACE-TR-1", SCHOOL_XINGHE, "stu_race", "MATH", 4)
            issue_certificate(
                conn,
                IssueIn(
                    certificate_code="CERT-RACE-1",
                    definition_id=DEF_LINHAI_BIZ_DATA,
                    student_id="stu_race",
                    record_codes=["RACE-TR-1"],
                ),
            )
            training.create_term(
                conn,
                TrainingTermIn(
                    term_id="term_race",
                    organizer_school_id=SCHOOL_LINHAI,
                    title="并发期",
                    accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
                ),
            )
            training.create_class(
                conn,
                TrainingClassIn(
                    class_id="cls_race", term_id="term_race", name="竞赛班", capacity=1
                ),
            )
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_concurrent_enrollments_never_overbook_one_seat(self):
        workers = 10

        def attempt(index: int) -> tuple[str, str]:
            conn = connect(self.db_path)
            try:
                from app.models import EnrollmentIn, IssueIn as _IssueIn

                student_id = f"stu_w{index}"
                create_student(conn, student_id, f" worker{index}")
                record_code = f"RACE-W-{index}"
                create_record(
                    conn, record_code, SCHOOL_XINGHE, student_id, "MATH", 4
                )
                issue_certificate(
                    conn,
                    _IssueIn(
                        certificate_code=f"CERT-W-{index}",
                        definition_id=DEF_LINHAI_BIZ_DATA,
                        student_id=student_id,
                        record_codes=[record_code],
                    ),
                )
                training.enroll(
                    conn,
                    EnrollmentIn(
                        term_id="term_race",
                        student_id=student_id,
                        class_id="cls_race",
                        certificate_code=f"CERT-W-{index}",
                    ),
                )
                return "ok", student_id
            except ServiceError as exc:
                return "conflict", exc.code
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(attempt, range(workers)))
        oks = [item for item in outcomes if item[0] == "ok"]
        conflicts = [item for item in outcomes if item[0] == "conflict"]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(len(conflicts), workers - 1, outcomes)

        conn = connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM enrollment WHERE term_id = ? AND status = ?",
                ("term_race", "enrolled"),
            ).fetchone()["count"]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_concurrent_swap_orders_cannot_double_reserve_one_student(self):
        conn = connect(self.db_path)
        try:
            from app.models import EnrollmentIn, TrainingClassIn

            for student_id, class_id in [("stu_p", "cls_race"), ("stu_q", "cls_race2")]:
                create_student(conn, student_id, student_id)
                create_record(conn, f"REC-{student_id}", SCHOOL_XINGHE, student_id, "MATH", 4)
                issue_certificate(
                    conn,
                    IssueIn(
                        certificate_code=f"CERT-{student_id}",
                        definition_id=DEF_LINHAI_BIZ_DATA,
                        student_id=student_id,
                        record_codes=[f"REC-{student_id}"],
                    ),
                )
            training.create_class(
                conn,
                TrainingClassIn(
                    class_id="cls_race2", term_id="term_race", name="竞赛班2", capacity=5
                ),
            )
            training.enroll(
                conn,
                EnrollmentIn(
                    term_id="term_race",
                    student_id="stu_p",
                    class_id="cls_race",
                    certificate_code="CERT-stu_p",
                ),
            )
            training.enroll(
                conn,
                EnrollmentIn(
                    term_id="term_race",
                    student_id="stu_q",
                    class_id="cls_race2",
                    certificate_code="CERT-stu_q",
                ),
            )
        finally:
            conn.close()

        barrier = threading.Barrier(2)

        def submit(index: int) -> tuple[str, str]:
            from app.models import SwapOrderIn, SwapParticipantIn

            conn = connect(self.db_path)
            try:
                barrier.wait(timeout=10)
                training.create_swap_order(
                    conn,
                    SwapOrderIn(
                        order_id=f"SW-RACE-{index}",
                        term_id="term_race",
                        organizer_school_id=SCHOOL_LINHAI,
                        confirm_deadline=datetime.now(timezone.utc) + timedelta(hours=1),
                        participants=[
                            SwapParticipantIn(student_id="stu_p", to_class_id="cls_race2")
                        ],
                    ),
                )
                return "ok", f"SW-RACE-{index}"
            except ServiceError as exc:
                return "conflict", exc.code
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, range(2)))
        oks = [item for item in outcomes if item[0] == "ok"]
        self.assertEqual(len(oks), 1, outcomes)

        conn = connect(self.db_path)
        try:
            open_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM swap_participant
                WHERE student_id = 'stu_p' AND order_open = 1
                """
            ).fetchone()["count"]
            self.assertEqual(open_count, 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
