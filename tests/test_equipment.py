from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import equipment, training
from app.db import connect, reset_db_file
from app.main import create_app
from app.models import (
    EquipmentItemIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentRequirementIn,
    EquipmentRescheduleIn,
    EquipmentSuspensionIn,
    EquipmentTagsIn,
    TrainingClassIn,
    TrainingTermIn,
)
from app.seed import (
    DEF_LINHAI_BIZ_DATA,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
    seed_base_data,
)
from app.services import ServiceError

TERM = "term_equipment"
CLASS = "cls_equipment"
BASE = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)


def at(hours: int, minutes: int = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def item(item_id: str, tags: list[str], owner: str = SCHOOL_XINGHE) -> EquipmentItemIn:
    return EquipmentItemIn(
        item_id=item_id, owner_school_id=owner, name=item_id, tags=tags
    )


def requirement(tags: list[str], count: int = 1) -> EquipmentRequirementIn:
    return EquipmentRequirementIn(tags=tags, count=count)


class EquipmentTestBase(unittest.TestCase):
    """Service-level base with a controllable clock and a seeded term/class."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "equipment.db")
        self.app = create_app(self.db_path)
        self.client = TestClient(self.app)
        self.now = BASE
        self.conn = connect(self.db_path)
        equipment.set_clock(lambda: self.now)
        seed_base_data(self.conn)
        training.create_term(
            self.conn,
            TrainingTermIn(
                term_id=TERM,
                organizer_school_id=SCHOOL_LINHAI,
                title="共享器材实训",
                accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
            ),
        )
        training.create_class(
            self.conn,
            TrainingClassIn(
                class_id=CLASS, term_id=TERM, name="一班", capacity=20
            ),
        )

    def tearDown(self) -> None:
        equipment.set_clock(None)
        self.conn.close()
        self.client.close()
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def add_item(self, item_id: str, tags: list[str], owner: str = SCHOOL_XINGHE):
        return equipment.create_item(self.conn, item(item_id, tags, owner))

    def reserve(
        self,
        reservation_id: str,
        start: datetime,
        end: datetime,
        requirements: list[tuple[list[str], int]],
        organizer: str = SCHOOL_LINHAI,
        class_id: str = CLASS,
    ):
        return equipment.create_reservation(
            self.conn,
            EquipmentReservationIn(
                reservation_id=reservation_id,
                organizer_school_id=organizer,
                term_id=TERM,
                class_id=class_id,
                start_at=start,
                end_at=end,
                requirements=[requirement(tags, count) for tags, count in requirements],
            ),
        )

    def pickup(self, reservation_id: str, item_ids: list[str]):
        return equipment.pickup(
            self.conn,
            reservation_id,
            EquipmentPickupIn(
                organizer_school_id=SCHOOL_LINHAI, item_ids=item_ids
            ),
        )

    def reservation(self, reservation_id: str) -> dict:
        return equipment.get_reservation(
            self.conn, reservation_id, organizer_school_id=SCHOOL_LINHAI
        )

    def advance(self, hours: int, minutes: int = 0) -> None:
        self.now = at(hours, minutes)


class ItemMaintenanceTest(EquipmentTestBase):
    def test_item_registration_tags_and_owner_only_maintenance(self):
        created = self.add_item("EQ-1", ["projector", "hdmi"])
        self.assertEqual(sorted(created["tags"]), ["hdmi", "projector"])

        # Another school may reserve the item, but only the owner may suspend it.
        with self.assertRaises(ServiceError) as caught:
            equipment.add_suspension(
                self.conn,
                "EQ-1",
                EquipmentSuspensionIn(
                    owner_school_id=SCHOOL_LINHAI,
                    start_at=at(1),
                    end_at=at(2),
                ),
            )
        self.assertEqual(caught.exception.code, "not_item_owner")

        with self.assertRaises(ServiceError) as caught:
            equipment.set_item_tags(
                self.conn,
                "EQ-1",
                EquipmentTagsIn(
                    owner_school_id=SCHOOL_YUNSONG, tags=["projector"]
                ),
            )
        self.assertEqual(caught.exception.code, "not_item_owner")

        # The owner may update the capability tags.
        updated = equipment.set_item_tags(
            self.conn,
            "EQ-1",
            EquipmentTagsIn(owner_school_id=SCHOOL_XINGHE, tags=["projector"]),
        )
        self.assertEqual(updated["tags"], ["projector"])

        suspended = equipment.add_suspension(
            self.conn,
            "EQ-1",
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE,
                start_at=at(1),
                end_at=at(2),
                reason="maintenance",
            ),
        )
        suspension_id = suspended["created_suspension_id"]
        self.assertEqual(len(suspended["suspensions"]), 1)

        lifted = equipment.lift_suspension(
            self.conn, "EQ-1", suspension_id, SCHOOL_XINGHE
        )
        self.assertIsNotNone(lifted["suspensions"][0]["lifted_at"])


class ReservationRuleTest(EquipmentTestBase):
    def test_reservation_requires_own_class_and_full_set(self):
        self.add_item("EQ-1", ["projector"])

        with self.assertRaises(ServiceError) as caught:
            self.reserve(
                "R-WRONG-SCHOOL", at(1), at(2), [(["projector"], 1)],
                organizer=SCHOOL_YUNSONG,
            )
        self.assertEqual(caught.exception.code, "not_term_organizer")

        with self.assertRaises(ServiceError) as caught:
            self.reserve(
                "R-WRONG-CLASS", at(1), at(2), [(["projector"], 1)],
                class_id="cls_missing",
            )
        self.assertEqual(caught.exception.code, "class_not_found")

        # Need two units but only one matching physical item: refused, no row.
        with self.assertRaises(ServiceError) as caught:
            self.reserve("R-SHORT", at(1), at(2), [(["projector"], 2)])
        self.assertEqual(caught.exception.code, "reservation_not_feasible")
        self.assertEqual(
            caught.exception.details["shortage"]["total_required"], 2
        )
        listed = equipment.list_reservations(
            self.conn, organizer_school_id=SCHOOL_LINHAI
        )
        self.assertEqual(listed["reservations"], [])

        # A requirement needs ALL of its tags on one physical item.
        self.add_item("EQ-2", ["projector"])
        with self.assertRaises(ServiceError) as caught:
            self.reserve(
                "R-ALL-TAGS", at(1), at(2), [(["projector", "hdmi"], 1)]
            )
        self.assertEqual(caught.exception.code, "reservation_not_feasible")
        shortage = caught.exception.details["shortage"]
        self.assertEqual(shortage["requirement_shortfalls"][0]["eligible_total"], 0)

        self.add_item("EQ-3", ["projector", "hdmi"])
        accepted = self.reserve(
            "R-OK", at(1), at(3), [(["projector", "hdmi"], 1)]
        )
        self.assertTrue(accepted["feasible"])
        self.assertEqual(accepted["planned_item_ids"], ["EQ-3"])

    def test_distinct_items_within_one_reservation_and_no_midway_swaps(self):
        self.add_item("EQ-1", ["camera"])
        self.add_item("EQ-2", ["camera"])
        accepted = self.reserve("R-ONE", at(1), at(3), [(["camera"], 2)])
        planned = accepted["planned_item_ids"]
        self.assertEqual(len(set(planned)), 2)

        # Time comes; listing the same physical item twice cannot fill two slots.
        self.advance(1)
        with self.assertRaises(ValidationError):
            EquipmentPickupIn(
                organizer_school_id=SCHOOL_LINHAI,
                item_ids=[planned[0], planned[0]],
            )
        # Bypassing the request schema still cannot double-use an item.
        with self.assertRaises(ServiceError) as caught:
            equipment.pickup(
                self.conn,
                "R-ONE",
                EquipmentPickupIn.model_construct(
                    organizer_school_id=SCHOOL_LINHAI,
                    item_ids=[planned[0], planned[0]],
                ),
            )
        self.assertEqual(caught.exception.code, "pickup_duplicate_items")

        # Listing an item without all required tags is refused.
        self.add_item("EQ-3", ["tripod"])
        with self.assertRaises(ServiceError) as caught:
            self.pickup("R-ONE", [planned[0], "EQ-3"])
        self.assertEqual(caught.exception.code, "pickup_incomplete_set")

        picked = self.pickup("R-ONE", planned)
        self.assertEqual(sorted(picked["picked_up_item_ids"]), sorted(planned))

        # An item handed out is never silently replaced later.
        self.assertEqual(
            sorted(self.reservation("R-ONE")["picked_up_item_ids"]),
            sorted(planned),
        )

    def test_cancel_and_expire_only_before_pickup(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R-CANCEL", at(1), at(3), [(["camera"], 1)])
        cancelled = equipment.cancel_reservation(
            self.conn, "R-CANCEL", SCHOOL_LINHAI
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["planned_item_ids"], [])

        with self.assertRaises(ServiceError) as caught:
            equipment.cancel_reservation(self.conn, "R-CANCEL", SCHOOL_LINHAI)
        self.assertEqual(caught.exception.code, "reservation_not_cancellable")

        # An accepted booking that reaches its end without pick-up expires.
        self.reserve("R-EXPIRE", at(4), at(5), [(["camera"], 1)])
        self.advance(5)
        expired = self.reservation("R-EXPIRE")
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["planned_item_ids"], [])


class SuspensionReplanTest(EquipmentTestBase):
    def test_suspension_replans_another_item_and_still_hands_over_whole_set(self):
        self.add_item("EQ-A", ["projector", "hdmi"])
        self.add_item("EQ-B", ["projector", "hdmi"])
        r1 = self.reserve("R1", at(1), at(3), [(["projector"], 1), (["hdmi"], 1)])
        self.assertEqual(r1["planned_item_ids"], ["EQ-A", "EQ-B"])

        # Suspend EQ-A exactly over R1: the plan must move to the remaining item.
        self.advance(1)
        equipment.add_suspension(
            self.conn,
            "EQ-A",
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE, start_at=at(1), end_at=at(3)
            ),
        )
        replanned = self.reservation("R1")
        # Two units need two distinct physical items, and EQ-A is suspended for
        # the whole remaining window, so even moving to EQ-B cannot keep R1 whole.
        self.assertTrue(replanned["short"])
        self.assertEqual(replanned["planned_item_ids"], [])
        self.assertEqual(
            {entry["item_id"] for entry in replanned["shortage"]["suspended_items"]},
            {"EQ-A"},
        )

        # Lifting the early suspension lets the same booking be kept again.
        suspension_id = self.conn.execute(
            "SELECT suspension_id FROM equipment_suspension WHERE item_id = 'EQ-A'"
        ).fetchone()["suspension_id"]
        equipment.lift_suspension(
            self.conn, "EQ-A", suspension_id, SCHOOL_XINGHE
        )
        restored = self.reservation("R1")
        self.assertTrue(restored["feasible"])

        # Handing over one item twice is impossible: every requirement needs its
        # own distinct physical item.
        with self.assertRaises(ServiceError) as caught:
            equipment.pickup(
                self.conn,
                "R1",
                EquipmentPickupIn.model_construct(
                    organizer_school_id=SCHOOL_LINHAI,
                    item_ids=["EQ-A", "EQ-A"],
                ),
            )
        self.assertEqual(caught.exception.code, "pickup_duplicate_items")
        picked = self.pickup("R1", ["EQ-A", "EQ-B"])
        self.assertEqual(sorted(picked["picked_up_item_ids"]), ["EQ-A", "EQ-B"])

    def test_lifted_suspension_restores_short_reservation_in_priority_order(self):
        self.add_item("EQ-A", ["camera"])
        self.add_item("EQ-B", ["camera"])
        r1 = self.reserve("R1", at(1), at(4), [(["camera"], 1)])
        r2 = self.reserve("R2", at(2), at(4), [(["camera"], 1)])
        self.assertNotEqual(r1["planned_item_ids"], r2["planned_item_ids"])

        self.advance(2)
        created = equipment.add_suspension(
            self.conn,
            r2["planned_item_ids"][0],
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE, start_at=at(2), end_at=at(4)
            ),
        )
        # Both bookings now compete for one physical item; the earlier one keeps it.
        self.assertIn("R2", created["affected_short_reservation_ids"])
        self.assertTrue(self.reservation("R1")["feasible"])
        short = self.reservation("R2")
        self.assertTrue(short["short"])
        self.assertEqual(short["planned_item_ids"], [])
        self.assertTrue(short["shortage"]["requirement_shortfalls"])

        # Lifting the suspension restores R2 without re-ordering anything.
        equipment.lift_suspension(
            self.conn,
            created["suspensions"][0]["item_id"],
            created["created_suspension_id"],
            SCHOOL_XINGHE,
        )
        restored = self.reservation("R2")
        self.assertTrue(restored["feasible"])
        self.assertFalse(restored["short"])


class PickupAndOverdueTest(EquipmentTestBase):
    def test_overdue_handover_makes_next_reservation_short_until_return(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(1), at(3), [(["camera"], 1)])
        self.reserve("R2", at(3), at(5), [(["camera"], 1)])
        self.advance(1)

        picked = self.pickup("R1", ["EQ-1"])
        self.assertEqual(picked["status"], "picked_up")
        self.assertEqual(picked["expected_release_at"], at(3).isoformat())

        # R1 is overdue; it keeps the physical item and loses its projected release.
        self.advance(3, 30)
        overdue = self.reservation("R1")
        self.assertEqual(overdue["status"], "picked_up")
        self.assertTrue(overdue["overdue"])
        self.assertIsNone(overdue["expected_release_at"])
        self.assertEqual(overdue["picked_up_item_ids"], ["EQ-1"])

        short = self.reservation("R2")
        self.assertTrue(short["short"])
        self.assertEqual(short["planned_item_ids"], [])
        self.assertEqual(short["status"], "accepted")

        # The physical return restores the next booking in place.
        returned = equipment.return_items(self.conn, "R1", SCHOOL_LINHAI)
        self.assertEqual(returned["status"], "returned")
        self.assertIn("R2", returned["restored_feasible_reservation_ids"])

        ready = self.reservation("R2")
        self.assertTrue(ready["feasible"])
        self.assertFalse(ready["short"])
        # The set can now be handed over inside R2's remaining window.
        picked_r2 = self.pickup("R2", ready["planned_item_ids"])
        self.assertEqual(picked_r2["picked_up_item_ids"], ["EQ-1"])

    def test_pickup_refused_outside_window_and_when_not_feasible(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(2), at(4), [(["camera"], 1)])

        # Too early, even though the item is planned and free.
        with self.assertRaises(ServiceError) as caught:
            self.pickup("R1", ["EQ-1"])
        self.assertEqual(caught.exception.code, "reservation_not_started")

        self.advance(2)
        equipment.add_suspension(
            self.conn,
            "EQ-1",
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE, start_at=at(2), end_at=at(4)
            ),
        )
        with self.assertRaises(ServiceError) as caught:
            self.pickup("R1", ["EQ-1"])
        self.assertEqual(caught.exception.code, "reservation_short")

        # Window end is exclusive: at ``end`` the booking has already expired
        # and can no longer be handed over.
        self.advance(4)
        self.assertEqual(self.reservation("R1")["status"], "expired")
        with self.assertRaises(ServiceError) as caught:
            self.pickup("R1", ["EQ-1"])
        self.assertEqual(caught.exception.code, "reservation_not_pickable")

    def test_suspension_does_not_cancel_an_inflight_handover(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(1), at(4), [(["camera"], 1)])
        self.advance(1)
        self.pickup("R1", ["EQ-1"])

        equipment.add_suspension(
            self.conn,
            "EQ-1",
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE, start_at=at(1), end_at=at(3)
            ),
        )
        still_out = self.reservation("R1")
        self.assertEqual(still_out["status"], "picked_up")
        self.assertEqual(still_out["picked_up_item_ids"], ["EQ-1"])

        # The picked-up set is returned as a whole set.
        returned = equipment.return_items(self.conn, "R1", SCHOOL_LINHAI)
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(returned["returned_item_ids"], ["EQ-1"])
        item_view = equipment.get_item(self.conn, "EQ-1")
        self.assertIsNone(item_view["current_handover"])

    def test_elapsed_time_is_not_required_again(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(0), at(3), [(["camera"], 1)])
        self.advance(2)
        # R1 only needs the remaining [2, 3). A later booking starting at 3 fits.
        accepted = self.reserve("R2", at(3), at(5), [(["camera"], 1)])
        self.assertTrue(accepted["feasible"])
        self.assertEqual(accepted["planned_item_ids"], ["EQ-1"])

        # Rescheduling R2 into R1's remaining window makes the later booking short.
        equipment.reschedule_reservation(
            self.conn,
            "R2",
            SCHOOL_LINHAI,
            start_at=at(2, 30),
            end_at=at(5),
        )
        short = self.reservation("R2")
        self.assertTrue(short["short"])


class ConcurrencyAndDurabilityTest(EquipmentTestBase):
    def test_concurrent_handover_of_same_item_happens_at_most_once(self):
        self.add_item("EQ-1", ["camera"])
        self.add_item("EQ-2", ["camera"])
        self.now = datetime.now(timezone.utc)
        start = self.now - timedelta(minutes=5)
        end = self.now + timedelta(hours=2)
        # Two overlapping reservations, each individually feasible with either item.
        self.reserve("R1", start, end, [(["camera"], 1)])
        self.reserve("R2", start, end, [(["camera"], 1)])

        barrier = threading.Barrier(2)
        results: list[str] = []

        def attempt(reservation_id: str) -> None:
            conn = connect(self.db_path)
            try:
                barrier.wait(timeout=5)
                try:
                    equipment.pickup(
                        conn,
                        reservation_id,
                        EquipmentPickupIn(
                            organizer_school_id=SCHOOL_LINHAI,
                            item_ids=["EQ-1"],
                        ),
                    )
                    results.append(f"{reservation_id}:ok")
                except ServiceError as exc:
                    results.append(f"{reservation_id}:{exc.code}")
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(attempt, ["R1", "R2"]))

        successes = [entry for entry in results if entry.endswith(":ok")]
        self.assertEqual(len(successes), 1, results)
        self.assertTrue(
            any("equipment_item_in_use" in entry for entry in results), results
        )

        # Exactly one live handover exists for the item in the database.
        live = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM equipment_allocation
            WHERE item_id = 'EQ-1'
              AND picked_up_at IS NOT NULL
              AND returned_at IS NULL
            """
        ).fetchone()["count"]
        self.assertEqual(live, 1)

    def test_restart_keeps_priority_order_and_actual_handover(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(1), at(3), [(["camera"], 1)])
        self.advance(1)
        self.pickup("R1", ["EQ-1"])

        self.conn.close()
        self.conn = connect(self.db_path)
        equipment.set_clock(lambda: self.now)

        row = self.conn.execute(
            "SELECT seq, status, picked_up_at FROM equipment_reservation "
            "WHERE reservation_id = 'R1'"
        ).fetchone()
        self.assertEqual(row["status"], "picked_up")
        self.assertIsNotNone(row["picked_up_at"])
        self.assertEqual(row["seq"], 1)

        evaluation = equipment.evaluate(self.conn)
        self.assertEqual(
            [r["item_id"] for r in equipment._allocations_for(self.conn, "R1")
             if r["picked_up_at"] is not None and r["returned_at"] is None],
            ["EQ-1"],
        )
        self.assertEqual(evaluation["expired_reservation_ids"], [])

    def test_http_endpoints_expose_board_and_held_numbers(self):
        # HTTP path uses the real clock, so keep windows around now.
        equipment.set_clock(None)
        now = datetime.now(timezone.utc)
        response = self.client.post(
            "/api/equipment/items",
            json={
                "item_id": "EQ-HTTP",
                "owner_school_id": SCHOOL_XINGHE,
                "tags": ["camera"],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

        resp = self.client.post(
            "/api/equipment/reservations",
            json={
                "reservation_id": "R-HTTP",
                "organizer_school_id": SCHOOL_LINHAI,
                "term_id": TERM,
                "class_id": CLASS,
                "start_at": (now - timedelta(minutes=1)).isoformat(),
                "end_at": (now + timedelta(hours=2)).isoformat(),
                "requirements": [{"tags": ["camera"], "count": 1}],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "accepted")

        picked = self.client.post(
            "/api/equipment/reservations/R-HTTP/pickup",
            json={
                "organizer_school_id": SCHOOL_LINHAI,
                "item_ids": ["EQ-HTTP"],
            },
        )
        self.assertEqual(picked.status_code, 200, picked.text)

        board = self.client.get(f"/api/equipment/schools/{SCHOOL_LINHAI}/board")
        self.assertEqual(board.status_code, 200, board.text)
        payload = board.json()
        self.assertEqual(payload["held_items"][0]["item_id"], "EQ-HTTP")
        self.assertEqual(payload["reservations"][0]["status"], "picked_up")

        returned = self.client.post(
            "/api/equipment/reservations/R-HTTP/return",
            json={"organizer_school_id": SCHOOL_LINHAI},
        )
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual(returned.json()["status"], "returned")
        self.assertEqual(
            self.client.get(f"/api/equipment/schools/{SCHOOL_LINHAI}/board")
            .json()["held_items"],
            [],
        )
        equipment.set_clock(lambda: self.now)


class PriorityAndNoSwapTest(EquipmentTestBase):
    def test_earlier_booking_is_never_displaced_by_a_later_one(self):
        self.add_item("EQ-1", ["camera"])
        first = self.reserve("R1", at(1), at(4), [(["camera"], 1)])
        # A disjoint window is fine: both can plan the same physical item.
        after = self.reserve("R2", at(4), at(6), [(["camera"], 1)])
        self.assertEqual(first["planned_item_ids"], ["EQ-1"])
        self.assertEqual(after["planned_item_ids"], ["EQ-1"])

        # A later overlapping attempt is refused and does not take R1's plan.
        with self.assertRaises(ServiceError) as caught:
            self.reserve("R3", at(2), at(3), [(["camera"], 1)])
        self.assertEqual(caught.exception.code, "reservation_not_feasible")
        self.assertEqual(self.reservation("R1")["planned_item_ids"], ["EQ-1"])

    def test_later_pickup_choice_replans_earlier_booking_instead_of_shorting_it(self):
        self.add_item("EQ-1", ["camera"])
        self.add_item("EQ-2", ["camera"])
        # R1 is accepted first and planned for EQ-1 in [2, 4).
        first = self.reserve("R1", at(2), at(4), [(["camera"], 1)])
        self.assertEqual(first["planned_item_ids"], ["EQ-1"])
        # R2 is accepted later and planned for the other unit.
        second = self.reserve("R2", at(0), at(3), [(["camera"], 1)])
        self.assertEqual(second["planned_item_ids"], ["EQ-2"])

        # R2 hands over EQ-1 instead of its planned number. R1 is not declared
        # short just because its previous number was taken: its unclaimed plan
        # is re-arranged onto EQ-2.
        self.advance(0)
        picked = self.pickup("R2", ["EQ-1"])
        self.assertEqual(picked["picked_up_item_ids"], ["EQ-1"])
        later = self.reservation("R1")
        self.assertTrue(later["feasible"])
        self.assertEqual(later["planned_item_ids"], ["EQ-2"])

    def test_short_reservation_cannot_be_picked_up_until_overdue_item_returns(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(0), at(2), [(["camera"], 1)])
        self.reserve("R2", at(2), at(4), [(["camera"], 1)])
        self.advance(0)
        self.pickup("R1", ["EQ-1"])

        # R1 misses its agreed end; R2 starts and is short.
        self.advance(2, 30)
        short = self.reservation("R2")
        self.assertTrue(short["short"])
        with self.assertRaises(ServiceError) as caught:
            self.pickup("R2", ["EQ-1"])
        self.assertEqual(caught.exception.code, "reservation_short")

        equipment.return_items(self.conn, "R1", SCHOOL_LINHAI)
        ready = self.reservation("R2")
        self.assertTrue(ready["feasible"])
        picked = self.pickup("R2", ["EQ-1"])
        self.assertEqual(picked["picked_up_item_ids"], ["EQ-1"])

    def test_cancel_returns_capacity_to_a_short_later_booking(self):
        self.add_item("EQ-1", ["camera"])
        self.reserve("R1", at(1), at(4), [(["camera"], 1)])
        with self.assertRaises(ServiceError):
            self.reserve("R2", at(2), at(4), [(["camera"], 1)])

        equipment.cancel_reservation(self.conn, "R1", SCHOOL_LINHAI)
        accepted = self.reserve("R2", at(2), at(4), [(["camera"], 1)])
        self.assertTrue(accepted["feasible"])
        self.assertEqual(accepted["planned_item_ids"], ["EQ-1"])


class PriorityOrderRegressionTest(EquipmentTestBase):
    def test_priority_follows_seq_not_creation_after_a_cancellation(self):
        self.add_item("EQ-1", ["camera"])
        self.add_item("EQ-2", ["camera"])
        self.reserve("R1", at(1), at(3), [(["camera"], 1)])
        self.reserve("R2", at(1), at(3), [(["camera"], 1)])
        with self.assertRaises(ServiceError):
            self.reserve("R3", at(1), at(3), [(["camera"], 1)])

        # R1 leaves, so its seq slot does not make R3 exist. A new R4 takes
        # the next seq and is ranked after R2.
        equipment.cancel_reservation(self.conn, "R1", SCHOOL_LINHAI)
        fourth = self.reserve("R4", at(1), at(3), [(["camera"], 1)])
        self.assertEqual(fourth["seq"], 3)

        # Suspend the number R2 planned. R2 has the earlier seq and keeps a
        # complete plan; R4 is the one shown short.
        target = self.reservation("R2")["planned_item_ids"][0]
        equipment.add_suspension(
            self.conn,
            target,
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE, start_at=at(1), end_at=at(3)
            ),
        )
        later = self.reservation("R2")
        newest = self.reservation("R4")
        self.assertTrue(later["feasible"])
        self.assertNotIn(target, later["planned_item_ids"])
        self.assertTrue(newest["short"])
        self.assertEqual(newest["planned_item_ids"], [])
        self.assertEqual(newest["shortage"]["blocked_by_earlier_reservations"][0]["reservation_ids"], ["R2"])
