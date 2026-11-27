#!/usr/bin/env python3
"""Runnable walkthrough: cross-school reconciliation for advanced repair costs.

Storyline -- 垫付、更正、关账与在途资金
    星河（器材所属校）为实训期垫付了一笔维修费，临海（主办校）登记三校
    1:1:1 的分摊名单。费用经批准后按轮次关账；云杉的划款还在路上时账单被
    更正，多付的部分自动变成反方向欠款；反方向在途款未落地前不能新发划款，
    直到真实到账后财务才按新的可发款额结清。

Run:
    python examples/demo_settlement.py --db /tmp/settlement_demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import equipment, settlement, training  # noqa: E402
from app.db import connect, reset_db_file  # noqa: E402
from app.models import (  # noqa: E402
    EquipmentItemIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentRequirementIn,
    ExpenseCorrectionIn,
    ExpenseIn,
    ReceiptIn,
    ShareEntryIn,
    ShareRosterIn,
    StatementCloseIn,
    TrainingClassIn,
    TrainingTermIn,
    TransferIn,
)
from app.seed import (  # noqa: E402
    DEF_LINHAI_BIZ_DATA,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    SCHOOL_YUNSONG,
    seed_base_data,
)

TERM_ID = "term_settlement_demo"
CLASS_ID = "cls_settlement_demo"
ORGANIZER = SCHOOL_LINHAI  # 主办校
OWNER = SCHOOL_XINGHE  # 器材所属校 / 垫付校
THIRD = SCHOOL_YUNSONG


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-school reconciliation demo")
    parser.add_argument("--db", default="/tmp/settlement_demo.db", help="SQLite path")
    args = parser.parse_args()
    reset_db_file(args.db)
    conn = connect(args.db)
    try:
        seed_base_data(conn)
        training.create_term(
            conn,
            TrainingTermIn(
                term_id=TERM_ID,
                organizer_school_id=ORGANIZER,
                title="共享器材维修对账实训期",
                accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
            ),
        )
        training.create_class(
            conn,
            TrainingClassIn(class_id=CLASS_ID, term_id=TERM_ID, name="对账班", capacity=10),
        )

        # -- one camera is borrowed by the class and actually returned --------
        equipment.create_item(
            conn,
            EquipmentItemIn(
                item_id="it_camera",
                owner_school_id=OWNER,
                name="单反相机",
                tags=["camera"],
            ),
        )
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        end = datetime.now(timezone.utc) + timedelta(hours=2)
        equipment.create_reservation(
            conn,
            EquipmentReservationIn(
                reservation_id="res_demo",
                organizer_school_id=ORGANIZER,
                term_id=TERM_ID,
                class_id=CLASS_ID,
                start_at=start,
                end_at=end,
                requirements=[EquipmentRequirementIn(tags=["camera"], count=1)],
            ),
        )
        equipment.pickup(
            conn,
            "res_demo",
            EquipmentPickupIn(organizer_school_id=ORGANIZER, item_ids=["it_camera"]),
        )
        equipment.return_items(conn, "res_demo", ORGANIZER)

        # -- 1. organizer registers the 1:1:1 share roster --------------------
        roster = settlement.set_roster(
            conn,
            TERM_ID,
            ShareRosterIn(
                organizer_school_id=ORGANIZER,
                shares=[
                    ShareEntryIn(school_id=SCHOOL_XINGHE, share=1),
                    ShareEntryIn(school_id=SCHOOL_LINHAI, share=1),
                    ShareEntryIn(school_id=SCHOOL_YUNSONG, share=1),
                ],
            ),
        )
        show("1. 主办校登记分摊名单（1:1:1）", roster)

        # -- 2. owner advances 300.00 and reports the expense -----------------
        expense = settlement.create_expense(
            conn,
            TERM_ID,
            ExpenseIn(
                expense_id="exp_repair",
                advancing_school_id=OWNER,
                reservation_id="res_demo",
                item_id="it_camera",
                amount_cents=30000,
            ),
        )
        show("2. 器材所属校申报垫付 300.00（待批准，含分摊试算）", expense["versions"][0])
        assert expense["versions"][0]["status"] == "pending"

        # roster is frozen from the first report on
        try:
            settlement.set_roster(
                conn,
                TERM_ID,
                ShareRosterIn(
                    organizer_school_id=ORGANIZER,
                    shares=[ShareEntryIn(school_id=SCHOOL_XINGHE, share=1)],
                ),
            )
            raise AssertionError("roster change after reporting must fail")
        except Exception as exc:  # noqa: BLE001 - demo prints the domain error
            show("3. 开始报费后名单冻结", getattr(exc, "code", exc))

        # -- 4. organizer approves; unclosed approval does not move money -----
        settlement.approve_version(conn, "exp_repair", 1, ORGANIZER)
        view = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        assert view["payable_cents"] == 0
        show("4. 获批但尚未关账：可发款额仍为 0", view)

        # -- 5. close round 1: allocations are booked -------------------------
        statement1, _ = settlement.close_round(
            conn, TERM_ID, StatementCloseIn(organizer_school_id=ORGANIZER, round_no=1)
        )
        show("5. 第 1 轮关账：每校分摊 100.00", statement1)
        view = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        assert view["payable_cents"] == 10000

        # -- 6. yunsong wires its full share; the money is still travelling ---
        settlement.create_transfer(
            conn,
            TERM_ID,
            TransferIn(
                transfer_id="trf_yunsong_1",
                payer_school_id=THIRD,
                payee_school_id=OWNER,
                amount_cents=10000,
            ),
        )
        view = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        assert view["payable_cents"] == 0
        show("6. 云杉开出 100.00 划款单（在途），可发款额降为 0", view)

        # -- 7. the bill is corrected down while the money travels ------------
        settlement.create_correction(
            conn,
            "exp_repair",
            ExpenseCorrectionIn(
                advancing_school_id=OWNER, replaces_version=1, new_amount_cents=15000
            ),
        )
        settlement.approve_version(conn, "exp_repair", 2, ORGANIZER)
        statement2, _ = settlement.close_round(
            conn, TERM_ID, StatementCloseIn(organizer_school_id=ORGANIZER, round_no=2)
        )
        show("7. 更正为 150.00 并第 2 轮关账：按差额入账", statement2["lines"])

        # the in-flight transfer still records its real arrival
        settlement.create_receipt(
            conn,
            "trf_yunsong_1",
            ReceiptIn(receipt_id="rcp_1", payee_school_id=OWNER, amount_cents=10000),
        )
        view = settlement.get_payable(conn, TERM_ID, OWNER, THIRD)
        assert view["payable_cents"] == 5000
        show(
            "8. 在途款真实到账 100.00；多付的 50.00 成为反方向欠款",
            view,
        )

        # -- 9. xinghe refunds the overpayment --------------------------------
        settlement.create_transfer(
            conn,
            TERM_ID,
            TransferIn(
                transfer_id="trf_xinghe_back",
                payer_school_id=OWNER,
                payee_school_id=THIRD,
                amount_cents=5000,
            ),
        )

        # -- 10. the bill is corrected back up while the refund travels -------
        settlement.create_correction(
            conn,
            "exp_repair",
            ExpenseCorrectionIn(
                advancing_school_id=OWNER, replaces_version=2, new_amount_cents=33000
            ),
        )
        settlement.approve_version(conn, "exp_repair", 3, ORGANIZER)
        statement3, _ = settlement.close_round(
            conn, TERM_ID, StatementCloseIn(organizer_school_id=ORGANIZER, round_no=3)
        )
        view = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        assert view["blocked_by_reverse_inflight"]
        show(
            "9. 更正回 330.00 并第 3 轮关账：云杉又欠 10.00，"
            "但反方向 50.00 退款仍在路上，不能新发",
            view,
        )
        try:
            settlement.create_transfer(
                conn,
                TERM_ID,
                TransferIn(
                    transfer_id="trf_blocked",
                    payer_school_id=THIRD,
                    payee_school_id=OWNER,
                    amount_cents=1000,
                ),
            )
            raise AssertionError("issue during reverse in-flight must fail")
        except Exception as exc:  # noqa: BLE001
            show("10. 反方向在途期间新发被拒", getattr(exc, "code", exc))

        # -- 11. the refund arrives; the true balance can now be wired --------
        settlement.create_receipt(
            conn,
            "trf_xinghe_back",
            ReceiptIn(receipt_id="rcp_2", payee_school_id=THIRD, amount_cents=5000),
        )
        view = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        # yunsong paid 100, got 50 back, final share is 110 -> 60 left
        assert view["payable_cents"] == 6000
        show("11. 退款到账后：云杉实欠 60.00，可全额发款", view)
        settlement.create_transfer(
            conn,
            TERM_ID,
            TransferIn(
                transfer_id="trf_yunsong_2",
                payer_school_id=THIRD,
                payee_school_id=OWNER,
                amount_cents=6000,
            ),
        )
        settlement.create_receipt(
            conn,
            "trf_yunsong_2",
            ReceiptIn(receipt_id="rcp_3", payee_school_id=OWNER, amount_cents=6000),
        )
        final = settlement.get_payable(conn, TERM_ID, THIRD, OWNER)
        assert final["payable_cents"] == 0 and final["debt"] is None

        # -- 12. every closed round is kept immutable --------------------------
        rounds = settlement.list_statements(conn, TERM_ID)
        show("12. 历轮账单（关账后不得改写，重复关闭返回原账单）", rounds)
        replay, created = settlement.close_round(
            conn, TERM_ID, StatementCloseIn(organizer_school_id=ORGANIZER, round_no=3)
        )
        assert not created and replay == statement3
        show("13. 双方结清，第 3 轮账单快照保持关账时点的在途/到账", statement3["pairs"])
        print("\n全部断言通过：对账、更正、关账与在途资金的时间线完整。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
