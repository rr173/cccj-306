#!/usr/bin/env python3
"""Runnable walkthrough: one shared equipment room, several schools, one class.

Storyline A -- suspension forces a re-plan, the whole set is still handed out
    An owner school suspends one projector for exactly the class's slot. The
    organizer's booking is re-planned onto another physical projector with the
    same capability tags, and the class still collects every item in one go.

Storyline B -- overdue loan makes the next booking short, return restores it
    The first class takes the last camera and does not bring it back on the
    agreed end time. The next class's booking is shown as short (缺套) without
    holding any item; when the camera is physically returned the later booking
    is restored in its original priority position and can be collected.

Run:
    python examples/demo_equipment_room.py --db /tmp/equipment_demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import equipment, training  # noqa: E402
from app.db import connect, reset_db_file  # noqa: E402
from app.models import (  # noqa: E402
    EquipmentItemIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentRequirementIn,
    EquipmentSuspensionIn,
    TrainingClassIn,
    TrainingTermIn,
)
from app.seed import (  # noqa: E402
    DEF_LINHAI_BIZ_DATA,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    seed_base_data,
)

TERM_ID = "term_equipment_demo"
CLASS_ID = "cls_equipment_demo"
BASE = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)


def at(hours: float, minutes: float = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def reserve(conn, reservation_id: str, start, end, requirements):
    return equipment.create_reservation(
        conn,
        EquipmentReservationIn(
            reservation_id=reservation_id,
            organizer_school_id=SCHOOL_LINHAI,
            term_id=TERM_ID,
            class_id=CLASS_ID,
            start_at=start,
            end_at=end,
            requirements=[
                EquipmentRequirementIn(tags=tags, count=count)
                for tags, count in requirements
            ],
        ),
    )


def pickup(conn, reservation_id: str, item_ids: list[str]):
    return equipment.pickup(
        conn,
        reservation_id,
        EquipmentPickupIn(organizer_school_id=SCHOOL_LINHAI, item_ids=item_ids),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/tmp/equipment_demo.db")
    args = parser.parse_args()

    reset_db_file(args.db)
    conn = connect(args.db)
    now = [BASE]
    equipment.set_clock(lambda: now[0])
    try:
        seed_base_data(conn)

        # The organizer school reuses one of its own training classes.
        training.create_term(
            conn,
            TrainingTermIn(
                term_id=TERM_ID,
                organizer_school_id=SCHOOL_LINHAI,
                title="共享器材实训（器材室演示）",
                accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
            ),
        )
        training.create_class(
            conn,
            TrainingClassIn(
                class_id=CLASS_ID, term_id=TERM_ID, name="器材实训班", capacity=10
            ),
        )

        # The source school owns and registers every physical unit, with
        # capability tags. Numbers are physical: borrowing is by item_id.
        for item_id, tags in [
            ("PRJ-A", ["projector", "hdmi"]),
            ("PRJ-B", ["projector", "hdmi"]),
            ("HDMI-1", ["hdmi"]),
            ("CAM-A", ["camera"]),
        ]:
            equipment.create_item(
                conn,
                EquipmentItemIn(
                    item_id=item_id,
                    owner_school_id=SCHOOL_XINGHE,
                    name=item_id,
                    tags=tags,
                ),
            )

        # ------------------------------------------------------------------
        # Storyline A: a suspension re-plans onto another physical item
        # ------------------------------------------------------------------
        print("\n" + "#" * 70)
        print("# 情节 A：停用一台投影仪后重排，整套仍能一次领齐")
        print("#" * 70)

        booking_a = reserve(
            conn,
            "EQ-A-CLASS",
            at(1),
            at(3),
            [
                (["projector"], 1),
                (["hdmi"], 1),
                (["camera"], 1),
            ],
        )
        show(
            "A1) 主办校为班次预约整套器材，首次受理即给出完整计划",
            {
                "reservation_id": booking_a["reservation_id"],
                "status": booking_a["status"],
                "feasible": booking_a["feasible"],
                "planned_item_ids": booking_a["planned_item_ids"],
            },
        )
        assert booking_a["feasible"]
        # One physical unit per required piece; numbers are only planned here.
        assert set(booking_a["planned_item_ids"]) == {"PRJ-A", "HDMI-1", "CAM-A"}

        now[0] = at(1)
        suspended = equipment.add_suspension(
            conn,
            "PRJ-A",
            EquipmentSuspensionIn(
                owner_school_id=SCHOOL_XINGHE,
                start_at=at(1),
                end_at=at(3),
                reason="投影仪送检",
            ),
        )
        after = equipment.get_reservation(conn, "EQ-A-CLASS")
        show(
            "A2) 属校停用 PRJ-A（覆盖本班时段）；预约整套重排到 PRJ-B + HDMI-1 + CAM-A",
            {
                "affected_short_reservation_ids": suspended[
                    "affected_short_reservation_ids"
                ],
                "status": after["status"],
                "feasible": after["feasible"],
                "planned_item_ids": after["planned_item_ids"],
                "suspensions": suspended["suspensions"],
            },
        )
        assert after["feasible"]
        assert "PRJ-A" not in after["planned_item_ids"]
        assert set(after["planned_item_ids"]) == {"PRJ-B", "HDMI-1", "CAM-A"}

        # Early lift is allowed, but A already works with the alternate unit.
        lift = equipment.lift_suspension(
            conn,
            "PRJ-A",
            suspended["created_suspension_id"],
            SCHOOL_XINGHE,
        )
        assert lift["suspensions"][0]["lifted_at"] is not None

        # One handover lists all physical numbers actually exchanged.
        handed = pickup(
            conn, "EQ-A-CLASS", after["planned_item_ids"]
        )
        show(
            "A3) 进窗后一次登记实际交接的全部编号，整套领齐",
            {
                "status": handed["status"],
                "picked_up_item_ids": handed["picked_up_item_ids"],
                "expected_release_at": handed["expected_release_at"],
            },
        )
        assert handed["status"] == "picked_up"
        assert set(handed["picked_up_item_ids"]) == set(
            after["planned_item_ids"]
        )

        board = equipment.school_board(conn, SCHOOL_LINHAI)
        assert {row["item_id"] for row in board["held_items"]} == set(
            handed["picked_up_item_ids"]
        )

        # Return the set before the class ends.
        returned_a = equipment.return_items(conn, "EQ-A-CLASS", SCHOOL_LINHAI)
        show(
            "A4) 整套归还后单据状态与本校已领编号同步清空",
            {
                "status": returned_a["status"],
                "returned_item_ids": returned_a["returned_item_ids"],
                "held_items_now": equipment.school_board(conn, SCHOOL_LINHAI)[
                    "held_items"
                ],
            },
        )
        assert returned_a["status"] == "returned"
        assert equipment.school_board(conn, SCHOOL_LINHAI)["held_items"] == []

        # ------------------------------------------------------------------
        # Storyline B: overdue handover -> next booking short -> return restores
        # ------------------------------------------------------------------
        print("\n" + "#" * 70)
        print("# 情节 B：前一班逾期未还，后一班暂缺；归还后按原顺位恢复")
        print("#" * 70)

        now[0] = at(3)
        first = reserve(conn, "EQ-B-FIRST", at(3), at(5), [(["camera"], 1)])
        second = reserve(conn, "EQ-B-SECOND", at(5), at(7), [(["camera"], 1)])
        show(
            "B1) 两班先后预约同一台实物；都在受理时拿到完整计划",
            {
                "first": {
                    "seq": first["seq"],
                    "planned_item_ids": first["planned_item_ids"],
                },
                "second": {
                    "seq": second["seq"],
                    "planned_item_ids": second["planned_item_ids"],
                },
            },
        )
        assert first["planned_item_ids"] == ["CAM-A"]
        assert second["planned_item_ids"] == ["CAM-A"]

        picked_first = pickup(conn, "EQ-B-FIRST", ["CAM-A"])
        assert picked_first["status"] == "picked_up"

        # The first class never returns it. At 6:30 the camera is overdue.
        now[0] = at(6, 30)
        overdue = equipment.get_reservation(conn, "EQ-B-FIRST")
        short = equipment.get_reservation(conn, "EQ-B-SECOND")
        show(
            "B2) 逾期未还：不再预计释放时间；下一班显示缺套且不占器材",
            {
                "first": {
                    "status": overdue["status"],
                    "overdue": overdue["overdue"],
                    "expected_release_at": overdue["expected_release_at"],
                    "picked_up_item_ids": overdue["picked_up_item_ids"],
                },
                "second": {
                    "status": short["status"],
                    "feasible": short["feasible"],
                    "short": short["short"],
                    "planned_item_ids": short["planned_item_ids"],
                    "shortage": short["shortage"],
                },
            },
        )
        assert overdue["overdue"] is True
        assert overdue["expected_release_at"] is None
        assert short["short"] is True
        assert short["planned_item_ids"] == []
        assert short["shortage"]["handover_blocked_items"][0]["item_id"] == "CAM-A"

        # A new order that could not be kept in full is not accepted at all.
        try:
            reserve(conn, "EQ-B-THIRD", at(6, 45), at(8), [(["camera"], 1)])
        except Exception as exc:  # ServiceError
            show(
                "B3) 新单无法整套保留：不受理，也不占器材",
                {"code": exc.code, "message": exc.message},
            )
            assert exc.code == "reservation_not_feasible"
        else:  # pragma: no cover
            raise AssertionError("B3 expected reservation_not_feasible")

        # Physical return, well after the agreed end.
        restored = equipment.return_items(conn, "EQ-B-FIRST", SCHOOL_LINHAI)
        recovered = equipment.get_reservation(conn, "EQ-B-SECOND")
        show(
            "B4) 实际归还后，后一班按原顺位恢复完整计划",
            {
                "returned": {
                    "status": restored["status"],
                    "returned_item_ids": restored["returned_item_ids"],
                    "restored_feasible_reservation_ids": restored[
                        "restored_feasible_reservation_ids"
                    ],
                },
                "second": {
                    "seq": recovered["seq"],
                    "feasible": recovered["feasible"],
                    "short": recovered["short"],
                    "planned_item_ids": recovered["planned_item_ids"],
                },
            },
        )
        assert recovered["feasible"] is True
        assert recovered["planned_item_ids"] == ["CAM-A"]
        assert equipment.get_reservation(conn, "EQ-B-SECOND")["seq"] == second["seq"]

        # Within its (already running) window, the second class can now collect.
        handed_second = pickup(conn, "EQ-B-SECOND", ["CAM-A"])
        show(
            "B5) 条件恢复后在剩余时间内领出（不追补已经过去的用时）",
            {
                "status": handed_second["status"],
                "picked_up_item_ids": handed_second["picked_up_item_ids"],
                "start_at": handed_second["start_at"],
                "end_at": handed_second["end_at"],
            },
        )
        assert handed_second["picked_up_item_ids"] == ["CAM-A"]

        print("\nDEMO OK: 停用后重排领齐，逾期致后单暂缺、归还后恢复，均符合规则。")
    finally:
        equipment.set_clock(None)
        conn.close()


if __name__ == "__main__":
    main()
