"""Cross-school reconciliation tests: rosters, expense versions, statement
rounds, transfers, receipts, payable amounts, concurrency and restart."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import equipment, settlement, training
from app.db import connect
from app.main import create_app
from app.models import (
    EquipmentItemIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentRequirementIn,
    ReceiptIn,
    ShareEntryIn,
    ShareRosterIn,
    StatementCloseIn,
    TrainingClassIn,
    TrainingTermIn,
    TransferIn,
)
from app.seed import (
    DEF_LINHAI_BIZ_DATA,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
    seed_base_data,
)
from app.services import ServiceError

TERM = "term_settle"
TERM2 = "term_settle_2"
CLASS = "cls_settle"
CLASS2 = "cls_settle_2"
ORGANIZER = SCHOOL_LINHAI  # 主办校
OWNER = SCHOOL_XINGHE  # 器材所属校 / 垫付校
THIRD = SCHOOL_YUNSONG

ALL_SCHOOLS = [SCHOOL_LINHAI, SCHOOL_XINGHE, SCHOOL_YUNSONG]


def _window(hours: int = 2) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=hours), now + timedelta(hours=hours)


class SettlementTestBase(unittest.TestCase):
    """HTTP-level base: seeded schools, one term/class and camera items."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "settlement.db")
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        conn = connect(self.db_path)
        try:
            seed_base_data(conn)
            training.create_term(
                conn,
                TrainingTermIn(
                    term_id=TERM,
                    organizer_school_id=ORGANIZER,
                    title="跨校对账实训期",
                    accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
                ),
            )
            training.create_class(
                conn,
                TrainingClassIn(class_id=CLASS, term_id=TERM, name="对账班", capacity=10),
            )
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.client.close()
        self._tmp.cleanup()

    # -- equipment helpers ---------------------------------------------------

    def add_item(self, item_id: str, owner: str = OWNER) -> None:
        conn = connect(self.db_path)
        try:
            equipment.create_item(
                conn,
                EquipmentItemIn(
                    item_id=item_id, owner_school_id=owner, name=item_id, tags=["camera"]
                ),
            )
        finally:
            conn.close()

    def borrow_and_return(
        self,
        reservation_id: str,
        item_ids: list[str],
        term_id: str = TERM,
        class_id: str = CLASS,
    ) -> None:
        """Run one reservation through pickup and full return."""
        start, end = _window()
        conn = connect(self.db_path)
        try:
            equipment.create_reservation(
                conn,
                EquipmentReservationIn(
                    reservation_id=reservation_id,
                    organizer_school_id=ORGANIZER,
                    term_id=term_id,
                    class_id=class_id,
                    start_at=start,
                    end_at=end,
                    requirements=[
                        EquipmentRequirementIn(tags=["camera"], count=len(item_ids))
                    ],
                ),
            )
            equipment.pickup(
                conn,
                reservation_id,
                EquipmentPickupIn(
                    organizer_school_id=ORGANIZER, item_ids=list(item_ids)
                ),
            )
            equipment.return_items(conn, reservation_id, ORGANIZER)
        finally:
            conn.close()

    # -- settlement HTTP helpers ----------------------------------------------

    def set_roster(self, shares: list[tuple[str, int]], term_id: str = TERM, organizer: str = ORGANIZER):
        return self.client.put(
            f"/api/settlement/terms/{term_id}/roster",
            json={
                "organizer_school_id": organizer,
                "shares": [
                    {"school_id": school_id, "share": share}
                    for school_id, share in shares
                ],
            },
        )

    def default_roster(self, term_id: str = TERM):
        response = self.set_roster(
            [(SCHOOL_XINGHE, 1), (SCHOOL_LINHAI, 1), (SCHOOL_YUNSONG, 1)],
            term_id=term_id,
        )
        assert response.status_code == 200, response.json()
        return response

    def declare(
        self,
        reservation_id: str,
        item_id: str,
        amount_cents: int,
        advancing: str = OWNER,
        term_id: str = TERM,
        expense_id: str | None = None,
    ):
        payload = {
            "advancing_school_id": advancing,
            "reservation_id": reservation_id,
            "item_id": item_id,
            "amount_cents": amount_cents,
        }
        if expense_id is not None:
            payload["expense_id"] = expense_id
        return self.client.post(
            f"/api/settlement/terms/{term_id}/expenses", json=payload
        )

    def correct(self, expense_id: str, replaces_version: int, new_amount: int, advancing: str = OWNER):
        return self.client.post(
            f"/api/settlement/expenses/{expense_id}/corrections",
            json={
                "advancing_school_id": advancing,
                "replaces_version": replaces_version,
                "new_amount_cents": new_amount,
            },
        )

    def approve(self, expense_id: str, version_no: int, organizer: str = ORGANIZER):
        return self.client.post(
            f"/api/settlement/expenses/{expense_id}/versions/{version_no}/approve",
            json={"organizer_school_id": organizer},
        )

    def close_round(self, round_no: int, term_id: str = TERM, organizer: str = ORGANIZER):
        return self.client.post(
            f"/api/settlement/terms/{term_id}/close",
            json={"organizer_school_id": organizer, "round_no": round_no},
        )

    def transfer(self, transfer_id: str, payer: str, payee: str, amount: int, term_id: str = TERM):
        return self.client.post(
            f"/api/settlement/terms/{term_id}/transfers",
            json={
                "transfer_id": transfer_id,
                "payer_school_id": payer,
                "payee_school_id": payee,
                "amount_cents": amount,
            },
        )

    def receipt(self, transfer_id: str, receipt_id: str, payee: str, amount: int):
        return self.client.post(
            f"/api/settlement/transfers/{transfer_id}/receipts",
            json={
                "receipt_id": receipt_id,
                "payee_school_id": payee,
                "amount_cents": amount,
            },
        )

    def close_transfer(self, transfer_id: str, payee: str):
        return self.client.post(
            f"/api/settlement/transfers/{transfer_id}/close",
            json={"payee_school_id": payee},
        )

    def payable(self, payer: str, payee: str, term_id: str = TERM) -> dict:
        response = self.client.get(
            f"/api/settlement/terms/{term_id}/payable",
            params={"payer_school_id": payer, "payee_school_id": payee},
        )
        assert response.status_code == 200, response.json()
        return response.json()

    def statement(self, round_no: int, term_id: str = TERM) -> dict:
        response = self.client.get(
            f"/api/settlement/terms/{term_id}/statements/{round_no}"
        )
        assert response.status_code == 200, response.json()
        return response.json()

    def expense(self, expense_id: str) -> dict:
        response = self.client.get(f"/api/settlement/expenses/{expense_id}")
        assert response.status_code == 200, response.json()
        return response.json()

    # -- composite helpers ------------------------------------------------------

    def approved_expense(
        self,
        reservation_id: str,
        item_id: str,
        amount: int,
        expense_id: str = "exp_1",
        advancing: str = OWNER,
        term_id: str = TERM,
    ) -> str:
        response = self.declare(
            reservation_id, item_id, amount, advancing=advancing,
            term_id=term_id, expense_id=expense_id,
        )
        assert response.status_code == 201, response.json()
        response = self.approve(expense_id, 1)
        assert response.status_code == 200, response.json()
        return expense_id


class RosterTest(SettlementTestBase):
    def test_set_get_and_replace_before_reporting(self):
        response = self.set_roster([(SCHOOL_XINGHE, 2), (SCHOOL_LINHAI, 1)])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_shares"], 3)
        self.assertFalse(body["frozen"])
        self.assertEqual(
            [(s["school_id"], s["share"]) for s in body["shares"]],
            [(SCHOOL_LINHAI, 1), (SCHOOL_XINGHE, 2)],
        )
        # Replace wholesale while no expense exists yet.
        response = self.set_roster(
            [(SCHOOL_XINGHE, 1), (SCHOOL_LINHAI, 1), (SCHOOL_YUNSONG, 1)]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_shares"], 3)
        fetched = self.client.get(f"/api/settlement/terms/{TERM}/roster").json()
        self.assertEqual(len(fetched["shares"]), 3)

    def test_only_organizer_may_set_roster(self):
        response = self.set_roster([(SCHOOL_XINGHE, 1)], organizer=SCHOOL_XINGHE)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_term_organizer")

    def test_roster_validation(self):
        # share must be a positive integer
        response = self.set_roster([(SCHOOL_XINGHE, 0)])
        self.assertEqual(response.status_code, 422)
        # duplicate schools rejected
        response = self.client.put(
            f"/api/settlement/terms/{TERM}/roster",
            json={
                "organizer_school_id": ORGANIZER,
                "shares": [
                    {"school_id": SCHOOL_XINGHE, "share": 1},
                    {"school_id": SCHOOL_XINGHE, "share": 2},
                ],
            },
        )
        self.assertEqual(response.status_code, 422)
        # unknown school rejected
        response = self.set_roster([("sch_ghost", 1)])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "school_not_found")
        # unknown term
        response = self.set_roster([(SCHOOL_XINGHE, 1)], term_id="term_ghost")
        self.assertEqual(response.status_code, 404)

    def test_roster_frozen_once_reporting_starts(self):
        self.default_roster()
        self.add_item("it_cam1")
        self.borrow_and_return("res_1", ["it_cam1"])
        response = self.declare("res_1", "it_cam1", 100)
        self.assertEqual(response.status_code, 201)
        response = self.set_roster([(SCHOOL_XINGHE, 1)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "roster_frozen")
        fetched = self.client.get(f"/api/settlement/terms/{TERM}/roster").json()
        self.assertTrue(fetched["frozen"])
        self.assertEqual(len(fetched["shares"]), 3)


class ExpenseDeclarationTest(SettlementTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.default_roster()
        self.add_item("it_cam1")
        self.add_item("it_cam2")

    def test_declare_on_returned_borrow(self):
        self.borrow_and_return("res_1", ["it_cam1"])
        response = self.declare("res_1", "it_cam1", 100, expense_id="exp_1")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["expense_id"], "exp_1")
        self.assertEqual(body["advancing_school_id"], OWNER)
        self.assertIsNone(body["current_approved_version_no"])
        self.assertEqual(len(body["versions"]), 1)
        version = body["versions"][0]
        self.assertEqual(version["kind"], "original")
        self.assertEqual(version["status"], "pending")
        self.assertEqual(version["amount_cents"], 100)

    def test_allocation_largest_remainder_with_tie_break(self):
        self.borrow_and_return("res_1", ["it_cam1"])
        # 100 cents over three equal shares: 33/33/33 plus one leftover cent
        # going to the smallest school id on the tie.
        response = self.declare("res_1", "it_cam1", 100, expense_id="exp_1")
        allocations = {
            row["school_id"]: row["amount_cents"]
            for row in response.json()["versions"][0]["allocations"]
        }
        self.assertEqual(
            allocations,
            {SCHOOL_LINHAI: 34, SCHOOL_XINGHE: 33, SCHOOL_YUNSONG: 33},
        )

    def test_allocation_weighted_shares(self):
        self.set_roster(
            [(SCHOOL_XINGHE, 2), (SCHOOL_LINHAI, 1), (SCHOOL_YUNSONG, 1)]
        )
        self.add_item("it_cam9")
        self.borrow_and_return("res_9", ["it_cam9"])
        response = self.declare("res_9", "it_cam9", 5, expense_id="exp_9")
        allocations = {
            row["school_id"]: row["amount_cents"]
            for row in response.json()["versions"][0]["allocations"]
        }
        # exact shares: 2.5 / 1.25 / 1.25 -> floors 2/1/1, leftover cent to the
        # largest remainder (xinghe, 0.5).
        self.assertEqual(
            allocations,
            {SCHOOL_XINGHE: 3, SCHOOL_LINHAI: 1, SCHOOL_YUNSONG: 1},
        )

    def test_declare_requires_returned_borrow(self):
        start, end = _window()
        conn = connect(self.db_path)
        try:
            equipment.create_reservation(
                conn,
                EquipmentReservationIn(
                    reservation_id="res_out",
                    organizer_school_id=ORGANIZER,
                    term_id=TERM,
                    class_id=CLASS,
                    start_at=start,
                    end_at=end,
                    requirements=[EquipmentRequirementIn(tags=["camera"], count=1)],
                ),
            )
            equipment.pickup(
                conn,
                "res_out",
                EquipmentPickupIn(organizer_school_id=ORGANIZER, item_ids=["it_cam1"]),
            )
        finally:
            conn.close()
        response = self.declare("res_out", "it_cam1", 100)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "borrow_not_returned")

    def test_declare_requires_item_actually_borrowed(self):
        self.borrow_and_return("res_1", ["it_cam1"])
        response = self.declare("res_1", "it_cam2", 100)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "item_not_borrowed")

    def test_declare_role_checks(self):
        self.borrow_and_return("res_1", ["it_cam1"])
        # advancing school must own the item
        response = self.declare("res_1", "it_cam1", 100, advancing=THIRD)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_item_owner")
        # advancing school must be on the roster
        conn = connect(self.db_path)
        try:
            from app.services import create_school

            create_school(conn, "sch_outside", "校外学校")
        finally:
            conn.close()
        response = self.declare("res_1", "it_cam1", 100, advancing="sch_outside")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_in_roster")

    def test_one_expense_per_item_per_borrowing(self):
        self.borrow_and_return("res_1", ["it_cam1"])
        response = self.declare("res_1", "it_cam1", 100, expense_id="exp_1")
        self.assertEqual(response.status_code, 201)
        response = self.declare("res_1", "it_cam1", 200, expense_id="exp_2")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "expense_exists")
        self.assertEqual(response.json()["error"]["details"]["expense_id"], "exp_1")
        # The same item borrowed again in another reservation is a new expense.
        self.borrow_and_return("res_2", ["it_cam1"])
        response = self.declare("res_2", "it_cam1", 200, expense_id="exp_3")
        self.assertEqual(response.status_code, 201)

    def test_expense_id_conflict(self):
        self.borrow_and_return("res_1", ["it_cam1", "it_cam2"])
        response = self.declare("res_1", "it_cam1", 100, expense_id="exp_1")
        self.assertEqual(response.status_code, 201)
        response = self.declare("res_1", "it_cam2", 100, expense_id="exp_1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "expense_id_exists")

    def test_reservation_must_belong_to_term(self):
        conn = connect(self.db_path)
        try:
            training.create_term(
                conn,
                TrainingTermIn(
                    term_id=TERM2,
                    organizer_school_id=ORGANIZER,
                    title="另一期",
                    accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
                ),
            )
            training.create_class(
                conn,
                TrainingClassIn(class_id=CLASS2, term_id=TERM2, name="二班", capacity=5),
            )
        finally:
            conn.close()
        self.borrow_and_return("res_other", ["it_cam1"], term_id=TERM2, class_id=CLASS2)
        response = self.declare("res_other", "it_cam1", 100, term_id=TERM)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"], "reservation_term_mismatch"
        )


class ApprovalAndCorrectionTest(SettlementTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.default_roster()
        self.add_item("it_cam1")
        self.borrow_and_return("res_1", ["it_cam1"])
        response = self.declare("res_1", "it_cam1", 300, expense_id="exp_1")
        assert response.status_code == 201, response.json()

    def test_only_organizer_approves(self):
        response = self.approve("exp_1", 1, organizer=OWNER)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_term_organizer")

    def test_approve_and_double_approve(self):
        response = self.approve("exp_1", 1)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_approved_version_no"], 1)
        self.assertEqual(body["current_approved_amount_cents"], 300)
        self.assertEqual(body["versions"][0]["approved_by"], ORGANIZER)
        response = self.approve("exp_1", 1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "version_already_approved")

    def test_correction_replaces_amount_and_can_zero_it(self):
        self.approve("exp_1", 1)
        response = self.correct("exp_1", 1, 150)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(len(body["versions"]), 2)
        correction = body["versions"][1]
        self.assertEqual(correction["kind"], "correction")
        self.assertEqual(correction["replaces_version"], 1)
        self.assertEqual(correction["status"], "pending")
        self.assertEqual(
            {a["school_id"]: a["amount_cents"] for a in correction["allocations"]},
            {SCHOOL_LINHAI: 50, SCHOOL_XINGHE: 50, SCHOOL_YUNSONG: 50},
        )
        response = self.approve("exp_1", 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_approved_amount_cents"], 150)
        # correct down to zero
        response = self.correct("exp_1", 2, 0)
        self.assertEqual(response.status_code, 201)
        response = self.approve("exp_1", 3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["current_approved_amount_cents"], 0)

    def test_correction_must_replace_latest_approved(self):
        self.approve("exp_1", 1)
        self.correct("exp_1", 1, 150)
        self.approve("exp_1", 2)
        # v1 is no longer the latest approved version.
        response = self.correct("exp_1", 1, 90)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_base_version")
        # a correction cannot replace a pending version either
        self.correct("exp_1", 2, 60)
        response = self.correct("exp_1", 3, 30)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "base_version_not_approved")

    def test_stale_pending_correction_cannot_be_approved_later(self):
        self.approve("exp_1", 1)
        self.correct("exp_1", 1, 150)  # becomes v2
        self.correct("exp_1", 1, 90)  # becomes v3, also targeting v1
        self.approve("exp_1", 2)
        response = self.approve("exp_1", 3)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_base_version")

    def test_correction_only_by_advancing_school(self):
        self.approve("exp_1", 1)
        response = self.correct("exp_1", 1, 150, advancing=THIRD)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_advancing_school")


class StatementRoundTest(SettlementTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.default_roster()
        self.add_item("it_cam1")
        self.add_item("it_cam2")
        self.borrow_and_return("res_1", ["it_cam1", "it_cam2"])

    def test_pending_expense_not_included_approved_unclosed_no_payable(self):
        self.declare("res_1", "it_cam1", 300, expense_id="exp_1")
        # pending: close round 1 -> empty statement
        response = self.close_round(1)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["lines"], [])
        self.assertEqual(response.json()["pairs"], [])
        # approve but do not close: payable stays zero
        self.approve("exp_1", 1)
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["payable_cents"], 0)
        self.assertIsNone(view["debt"])
        # close round 2: the approved version enters and payable appears
        response = self.close_round(2)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json()["lines"]), 1)
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["payable_cents"], 100)
        self.assertEqual(
            view["debt"],
            {
                "debtor_school_id": SCHOOL_LINHAI,
                "creditor_school_id": SCHOOL_XINGHE,
                "amount_cents": 100,
            },
        )

    def test_statement_lines_pairs_and_immutability(self):
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        response = self.close_round(1)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["round_no"], 1)
        self.assertEqual(body["closed_by"], ORGANIZER)
        self.assertEqual(len(body["lines"]), 1)
        line = body["lines"][0]
        self.assertEqual(line["kind"], "original")
        self.assertEqual(line["item_id"], "it_cam1")
        self.assertEqual(
            {a["school_id"]: a["delta_cents"] for a in line["allocations"]},
            {SCHOOL_LINHAI: 100, SCHOOL_XINGHE: 100, SCHOOL_YUNSONG: 100},
        )
        pairs = {(p["school_a"], p["school_b"]): p for p in body["pairs"]}
        pair = pairs[(SCHOOL_LINHAI, SCHOOL_XINGHE)]
        self.assertEqual(pair["charges_a_to_b_cents"], 100)
        self.assertEqual(pair["received_a_to_b_cents"], 0)
        self.assertEqual(
            pair["net_owed"],
            {
                "debtor_school_id": SCHOOL_LINHAI,
                "creditor_school_id": SCHOOL_XINGHE,
                "amount_cents": 100,
            },
        )
        # advancing school's own share is self-borne: no pair row with itself
        self.assertNotIn((SCHOOL_XINGHE, SCHOOL_XINGHE), pairs)

        # money moves after closing: the stored round must not change
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 100)
        self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 100)
        again = self.statement(1)
        pair_after = {
            (p["school_a"], p["school_b"]): p for p in again["pairs"]
        }[(SCHOOL_LINHAI, SCHOOL_XINGHE)]
        self.assertEqual(pair_after["received_a_to_b_cents"], 0)
        self.assertEqual(again["closed_at"], body["closed_at"])

        # round 2 carries the balance forward and snapshots the arrival
        response = self.close_round(2)
        self.assertEqual(response.status_code, 201)
        pair2 = {
            (p["school_a"], p["school_b"]): p for p in response.json()["pairs"]
        }[(SCHOOL_LINHAI, SCHOOL_XINGHE)]
        self.assertEqual(pair2["received_a_to_b_cents"], 100)
        self.assertIsNone(pair2["net_owed"])

    def test_repeat_close_returns_original_statement(self):
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        first = self.close_round(1)
        self.assertEqual(first.status_code, 201)
        # add more approved expenses and money; replay must ignore them
        self.approved_expense("res_1", "it_cam2", 900, expense_id="exp_2")
        replay = self.close_round(1)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        # rounds must be sequential
        response = self.close_round(3)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "round_out_of_order")
        # round 2 picks up only what was approved since round 1
        second = self.close_round(2)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            [line["expense_id"] for line in second.json()["lines"]], ["exp_2"]
        )

    def test_correction_books_delta_allocation(self):
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        self.close_round(1)
        self.correct("exp_1", 1, 150)
        self.approve("exp_1", 2)
        response = self.close_round(2)
        lines = response.json()["lines"]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["kind"], "correction")
        self.assertEqual(line["replaces_version"], 1)
        self.assertEqual(line["amount_cents"], 150)
        self.assertEqual(
            {a["school_id"]: a["delta_cents"] for a in line["allocations"]},
            {SCHOOL_LINHAI: -50, SCHOOL_XINGHE: -50, SCHOOL_YUNSONG: -50},
        )
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["payable_cents"], 50)

    def test_close_only_by_organizer(self):
        response = self.close_round(1, organizer=OWNER)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_term_organizer")

    def test_list_statements(self):
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        self.close_round(1)
        self.close_round(2)
        response = self.client.get(f"/api/settlement/terms/{TERM}/statements")
        self.assertEqual(response.status_code, 200)
        statements = response.json()["statements"]
        self.assertEqual([s["round_no"] for s in statements], [1, 2])
        self.assertEqual(statements[0]["line_count"], 1)
        self.assertEqual(statements[1]["line_count"], 0)


class TransferAndReceiptTest(SettlementTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.default_roster()
        self.add_item("it_cam1")
        self.borrow_and_return("res_1", ["it_cam1"])
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        self.close_round(1)

    def test_transfer_requires_actual_debt(self):
        response = self.transfer("trf_x", SCHOOL_XINGHE, SCHOOL_LINHAI, 10)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "no_payable_debt")

    def test_transfer_capped_by_payable_and_inflight(self):
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["payable_cents"], 100)
        response = self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 101)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "amount_exceeds_payable")
        response = self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 60)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["outstanding_cents"], 60)
        # in-flight money reduces what may still be issued
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["inflight_forward_cents"], 60)
        self.assertEqual(view["payable_cents"], 40)
        response = self.transfer("trf_2", SCHOOL_LINHAI, SCHOOL_XINGHE, 41)
        self.assertEqual(response.status_code, 409)
        response = self.transfer("trf_2", SCHOOL_LINHAI, SCHOOL_XINGHE, 40)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)["payable_cents"], 0)

    def test_transfer_id_resendable(self):
        first = self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 60)
        self.assertEqual(first.status_code, 201)
        replay = self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 60)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        conflict = self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 61)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "transfer_id_conflict")
        listed = self.client.get(
            f"/api/settlement/terms/{TERM}/transfers",
            params={"school_id": SCHOOL_LINHAI},
        ).json()["transfers"]
        self.assertEqual(len(listed), 1)

    def test_partial_receipts_capped_by_face(self):
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 100)
        response = self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 40)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["received_cents"], 40)
        response = self.receipt("trf_1", "rcp_2", SCHOOL_XINGHE, 61)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "receipt_exceeds_face")
        response = self.receipt("trf_1", "rcp_2", SCHOOL_XINGHE, 60)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["received_cents"], 100)
        self.assertEqual(body["outstanding_cents"], 0)
        self.assertEqual(self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)["payable_cents"], 0)

    def test_receipt_id_resendable(self):
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 100)
        first = self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 40)
        self.assertEqual(first.status_code, 201)
        replay = self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 40)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        conflict = self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 41)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "receipt_id_conflict")
        body = self.client.get("/api/settlement/transfers/trf_1").json()
        self.assertEqual(len(body["receipts"]), 1)

    def test_receipt_only_by_payee(self):
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 100)
        response = self.receipt("trf_1", "rcp_1", SCHOOL_LINHAI, 40)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_transfer_payee")

    def test_close_transfer_releases_outstanding(self):
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 60)
        self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 20)
        # only the payee may close
        response = self.close_transfer("trf_1", SCHOOL_LINHAI)
        self.assertEqual(response.status_code, 403)
        response = self.close_transfer("trf_1", SCHOOL_XINGHE)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "closed")
        self.assertEqual(body["received_cents"], 20)
        # the unreceived 40 is released: payable = 100 - 20 arrived = 80
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["inflight_forward_cents"], 0)
        self.assertEqual(view["payable_cents"], 80)
        # no new receipts accepted, closing again is a no-op
        response = self.receipt("trf_1", "rcp_2", SCHOOL_XINGHE, 10)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "transfer_closed")
        response = self.close_transfer("trf_1", SCHOOL_XINGHE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["received_cents"], 20)

    def test_pair_netting_within_term(self):
        # linhai also advances an expense (as equipment owner) of 150 -> 50 each
        self.add_item("it_cam_l", owner=SCHOOL_LINHAI)
        self.borrow_and_return("res_l", ["it_cam_l"])
        self.approved_expense(
            "res_l", "it_cam_l", 150, expense_id="exp_l", advancing=SCHOOL_LINHAI
        )
        self.close_round(2)
        # linhai owes xinghe 100, xinghe owes linhai 50 -> net 50
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        self.assertEqual(view["charges_forward_cents"], 100)
        self.assertEqual(view["charges_reverse_cents"], 50)
        self.assertEqual(view["payable_cents"], 50)
        response = self.transfer("trf_n", SCHOOL_LINHAI, SCHOOL_XINGHE, 51)
        self.assertEqual(response.status_code, 409)
        response = self.transfer("trf_n", SCHOOL_LINHAI, SCHOOL_XINGHE, 50)
        self.assertEqual(response.status_code, 201)

    def test_no_netting_across_terms(self):
        # a second term with its own roster and expense
        conn = connect(self.db_path)
        try:
            training.create_term(
                conn,
                TrainingTermIn(
                    term_id=TERM2,
                    organizer_school_id=ORGANIZER,
                    title="另一期",
                    accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
                ),
            )
            training.create_class(
                conn,
                TrainingClassIn(class_id=CLASS2, term_id=TERM2, name="二班", capacity=5),
            )
        finally:
            conn.close()
        self.default_roster(term_id=TERM2)
        self.add_item("it_cam2")
        self.borrow_and_return("res_2", ["it_cam2"], term_id=TERM2, class_id=CLASS2)
        self.approved_expense("res_2", "it_cam2", 90, expense_id="exp_t2", term_id=TERM2)
        self.close_round(1, term_id=TERM2)
        # term 2 debt is independent: 30 each
        view = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE, term_id=TERM2)
        self.assertEqual(view["payable_cents"], 30)
        # term 1 unchanged
        self.assertEqual(
            self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)["payable_cents"], 100
        )


class CorrectionWhileMoneyInFlightTest(SettlementTestBase):
    """The headline scenario: corrections land while transfers travel."""

    def setUp(self) -> None:
        super().setUp()
        self.default_roster()
        self.add_item("it_cam1")
        self.borrow_and_return("res_1", ["it_cam1"])
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        self.close_round(1)

    def test_correction_does_not_cancel_or_block_real_arrivals(self):
        # yunsong sends its full 100 share; the money is still on the way
        self.transfer("trf_1", SCHOOL_YUNSONG, SCHOOL_XINGHE, 100)
        # the bill is corrected down to 150 (50 per school) and closed
        self.correct("exp_1", 1, 150)
        self.approve("exp_1", 2)
        self.close_round(2)
        # the open transfer still records its real arrival in full
        response = self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 100)
        self.assertEqual(response.status_code, 201)
        # the overpaid 50 becomes debt in the reverse direction
        view = self.payable(SCHOOL_XINGHE, SCHOOL_YUNSONG)
        self.assertEqual(view["actual_debt_cents"], 50)
        self.assertEqual(view["payable_cents"], 50)
        view = self.payable(SCHOOL_YUNSONG, SCHOOL_XINGHE)
        self.assertEqual(view["actual_debt_cents"], 0)
        self.assertEqual(view["payable_cents"], 0)
        # the pair's only debt now runs from xinghe back to yunsong
        self.assertEqual(
            view["debt"],
            {
                "debtor_school_id": SCHOOL_XINGHE,
                "creditor_school_id": SCHOOL_YUNSONG,
                "amount_cents": 50,
            },
        )

    def test_reverse_inflight_blocks_new_issue_until_resolved(self):
        self.transfer("trf_1", SCHOOL_YUNSONG, SCHOOL_XINGHE, 100)
        self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 100)
        # correct down to 150: xinghe now owes yunsong 50 (overpayment)
        self.correct("exp_1", 1, 150)
        self.approve("exp_1", 2)
        self.close_round(2)
        response = self.transfer("trf_2", SCHOOL_XINGHE, SCHOOL_YUNSONG, 50)
        self.assertEqual(response.status_code, 201)
        # correct back up to 330 (110 each): yunsong owes xinghe 10 again,
        # but xinghe's 50 is still on the way in the reverse direction
        self.correct("exp_1", 2, 330)
        self.approve("exp_1", 3)
        self.close_round(3)
        view = self.payable(SCHOOL_YUNSONG, SCHOOL_XINGHE)
        self.assertEqual(view["actual_debt_cents"], 10)
        self.assertEqual(view["inflight_reverse_cents"], 50)
        self.assertTrue(view["blocked_by_reverse_inflight"])
        self.assertEqual(view["payable_cents"], 0)
        response = self.transfer("trf_3", SCHOOL_YUNSONG, SCHOOL_XINGHE, 10)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "reverse_inflight")
        # once the reverse money arrives, the true balance can be issued:
        # yunsong paid 100, got 50 back, and owes 110 in total -> 60 left
        self.receipt("trf_2", "rcp_2", SCHOOL_YUNSONG, 50)
        view = self.payable(SCHOOL_YUNSONG, SCHOOL_XINGHE)
        self.assertEqual(view["payable_cents"], 60)
        response = self.transfer("trf_3", SCHOOL_YUNSONG, SCHOOL_XINGHE, 60)
        self.assertEqual(response.status_code, 201)

    def test_balances_view_matches_pair_views(self):
        self.transfer("trf_1", SCHOOL_YUNSONG, SCHOOL_XINGHE, 70)
        self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 20)
        response = self.client.get(
            f"/api/settlement/terms/{TERM}/schools/{SCHOOL_YUNSONG}/balances"
        )
        self.assertEqual(response.status_code, 200)
        counterparts = {
            entry["counterpart_school_id"]: entry
            for entry in response.json()["counterparts"]
        }
        towards_xinghe = counterparts[SCHOOL_XINGHE]
        self.assertEqual(towards_xinghe["charges_out_cents"], 100)
        self.assertEqual(towards_xinghe["received_out_cents"], 20)
        self.assertEqual(towards_xinghe["inflight_out_cents"], 50)
        self.assertEqual(towards_xinghe["payable_out_cents"], 30)
        self.assertEqual(
            towards_xinghe["debt"],
            {
                "debtor_school_id": SCHOOL_YUNSONG,
                "creditor_school_id": SCHOOL_XINGHE,
                "amount_cents": 80,
            },
        )
        # a school outside the roster gets nothing
        response = self.client.get(
            f"/api/settlement/terms/{TERM}/schools/sch_outside/balances"
        )
        self.assertEqual(response.status_code, 403)


class RestartPersistenceTest(SettlementTestBase):
    def test_history_and_inflight_survive_restart(self):
        self.default_roster()
        self.add_item("it_cam1")
        self.borrow_and_return("res_1", ["it_cam1"])
        self.approved_expense("res_1", "it_cam1", 300, expense_id="exp_1")
        self.close_round(1)
        self.transfer("trf_1", SCHOOL_LINHAI, SCHOOL_XINGHE, 60)
        self.receipt("trf_1", "rcp_1", SCHOOL_XINGHE, 20)
        before_payable = self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE)
        before_statement = self.statement(1)
        self.client.close()

        # restart the app on the same database file
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        self.assertEqual(self.payable(SCHOOL_LINHAI, SCHOOL_XINGHE), before_payable)
        self.assertEqual(self.statement(1), before_statement)
        transfer = self.client.get("/api/settlement/transfers/trf_1").json()
        self.assertEqual(transfer["received_cents"], 20)
        self.assertEqual(transfer["outstanding_cents"], 40)
        self.assertEqual(transfer["status"], "open")
        # the in-flight transfer still caps new issues after the restart
        response = self.transfer("trf_2", SCHOOL_LINHAI, SCHOOL_XINGHE, 41)
        self.assertEqual(response.status_code, 409)
        response = self.transfer("trf_2", SCHOOL_LINHAI, SCHOOL_XINGHE, 40)
        self.assertEqual(response.status_code, 201)


class SettlementConcurrencyTest(unittest.TestCase):
    """Service-level races: every interleaving must serialize cleanly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "race.db")
        create_app(self.db_path)
        conn = connect(self.db_path)
        try:
            seed_base_data(conn)
            training.create_term(
                conn,
                TrainingTermIn(
                    term_id=TERM,
                    organizer_school_id=ORGANIZER,
                    title="并发对账期",
                    accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
                ),
            )
            training.create_class(
                conn,
                TrainingClassIn(class_id=CLASS, term_id=TERM, name="并发班", capacity=10),
            )
            settlement.set_roster(
                conn,
                TERM,
                ShareRosterIn(
                    organizer_school_id=ORGANIZER,
                    shares=[
                        ShareEntryIn(school_id=school_id, share=1)
                        for school_id in ALL_SCHOOLS
                    ],
                ),
            )
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _book_debt(self, amount: int = 300) -> None:
        """One approved and closed expense of ``amount`` from xinghe."""
        conn = connect(self.db_path)
        try:
            equipment.create_item(
                conn,
                EquipmentItemIn(
                    item_id="it_race", owner_school_id=OWNER, name="it_race", tags=["camera"]
                ),
            )
            start, end = _window()
            equipment.create_reservation(
                conn,
                EquipmentReservationIn(
                    reservation_id="res_race",
                    organizer_school_id=ORGANIZER,
                    term_id=TERM,
                    class_id=CLASS,
                    start_at=start,
                    end_at=end,
                    requirements=[EquipmentRequirementIn(tags=["camera"], count=1)],
                ),
            )
            equipment.pickup(
                conn,
                "res_race",
                EquipmentPickupIn(organizer_school_id=ORGANIZER, item_ids=["it_race"]),
            )
            equipment.return_items(conn, "res_race", ORGANIZER)
            from app.models import ExpenseIn

            settlement.create_expense(
                conn,
                TERM,
                ExpenseIn(
                    expense_id="exp_race",
                    advancing_school_id=OWNER,
                    reservation_id="res_race",
                    item_id="it_race",
                    amount_cents=amount,
                ),
            )
            settlement.approve_version(conn, "exp_race", 1, ORGANIZER)
            settlement.close_round(
                conn, TERM, StatementCloseIn(organizer_school_id=ORGANIZER, round_no=1)
            )
        finally:
            conn.close()

    def test_concurrent_transfers_never_exceed_payable(self):
        self._book_debt()
        workers = 8

        def attempt(index: int) -> str:
            conn = connect(self.db_path)
            try:
                settlement.create_transfer(
                    conn,
                    TERM,
                    TransferIn(
                        transfer_id=f"trf_{index}",
                        payer_school_id=SCHOOL_LINHAI,
                        payee_school_id=SCHOOL_XINGHE,
                        amount_cents=60,
                    ),
                )
                return "ok"
            except ServiceError as exc:
                return exc.code
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(attempt, range(workers)))
        # debt is 100; each transfer asks 60 -> exactly one may be issued
        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("amount_exceeds_payable"), workers - 1)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transfer_order "
                "WHERE term_id = ?",
                (TERM,),
            ).fetchone()["total"]
            self.assertLessEqual(total, 100)
        finally:
            conn.close()

    def test_concurrent_receipts_never_exceed_face(self):
        self._book_debt()
        conn = connect(self.db_path)
        try:
            settlement.create_transfer(
                conn,
                TERM,
                TransferIn(
                    transfer_id="trf_face",
                    payer_school_id=SCHOOL_LINHAI,
                    payee_school_id=SCHOOL_XINGHE,
                    amount_cents=100,
                ),
            )
        finally:
            conn.close()
        workers = 10

        def attempt(index: int) -> str:
            conn = connect(self.db_path)
            try:
                settlement.create_receipt(
                    conn,
                    "trf_face",
                    ReceiptIn(
                        receipt_id=f"rcp_{index}",
                        payee_school_id=SCHOOL_XINGHE,
                        amount_cents=30,
                    ),
                )
                return "ok"
            except ServiceError as exc:
                return exc.code
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(attempt, range(workers)))
        # face 100 with 30 per receipt -> exactly three fit
        self.assertEqual(outcomes.count("ok"), 3, outcomes)
        self.assertEqual(outcomes.count("receipt_exceeds_face"), workers - 3)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transfer_receipt "
                "WHERE transfer_id = ?",
                ("trf_face",),
            ).fetchone()["total"]
            self.assertEqual(total, 90)
        finally:
            conn.close()

    def test_concurrent_close_same_round_is_idempotent(self):
        self._book_debt()
        workers = 6

        def attempt(_index: int) -> tuple[bool, str]:
            conn = connect(self.db_path)
            try:
                _payload, created = settlement.close_round(
                    conn,
                    TERM,
                    StatementCloseIn(organizer_school_id=ORGANIZER, round_no=2),
                )
                return created, _payload["closed_at"]
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(attempt, range(workers)))
        created = [outcome for outcome in outcomes if outcome[0]]
        self.assertEqual(len(created), 1, outcomes)
        # every caller observes the very same statement
        self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
        conn = connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM statement WHERE term_id = ? AND round_no = 2",
                (TERM,),
            ).fetchone()["count"]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_concurrent_receipt_and_close_serialize(self):
        # 1800 cents booked -> each counterpart owes 600, enough headroom for
        # six 30-cent transfers plus their arrivals.
        self._book_debt(amount=1800)
        for round_index in range(6):
            transfer_id = f"trf_rc_{round_index}"
            conn = connect(self.db_path)
            try:
                settlement.create_transfer(
                    conn,
                    TERM,
                    TransferIn(
                        transfer_id=transfer_id,
                        payer_school_id=SCHOOL_LINHAI,
                        payee_school_id=SCHOOL_XINGHE,
                        amount_cents=30,
                    ),
                )
            finally:
                conn.close()

            def record_receipt() -> str:
                conn = connect(self.db_path)
                try:
                    settlement.create_receipt(
                        conn,
                        transfer_id,
                        ReceiptIn(
                            receipt_id=f"rcp_rc_{round_index}",
                            payee_school_id=SCHOOL_XINGHE,
                            amount_cents=30,
                        ),
                    )
                    return "receipt_ok"
                except ServiceError as exc:
                    return exc.code
                finally:
                    conn.close()

            def close() -> str:
                conn = connect(self.db_path)
                try:
                    settlement.close_transfer(conn, transfer_id, SCHOOL_XINGHE)
                    return "closed"
                finally:
                    conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda fn: fn(), [record_receipt, close]))
            conn = connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM transfer_order WHERE transfer_id = ?",
                    (transfer_id,),
                ).fetchone()
                received = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents), 0) AS total "
                    "FROM transfer_receipt WHERE transfer_id = ?",
                    (transfer_id,),
                ).fetchone()["total"]
            finally:
                conn.close()
            self.assertEqual(row["status"], "closed")
            self.assertIn("closed", outcomes)
            if "receipt_ok" in outcomes:
                # receipt won the race: it is fully booked before the close
                self.assertEqual(received, 30)
            else:
                # close won the race: the late receipt was rejected cleanly
                self.assertEqual(outcomes.count("transfer_closed"), 1)
                self.assertEqual(received, 0)


if __name__ == "__main__":
    unittest.main()
