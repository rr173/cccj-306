#!/usr/bin/env python3
"""Runnable training walkthrough: joint swap orders and mid-swap disqualification.

Storyline A -- full-class mutual swap
    Two capacity-1 classes are each full. Neither student can move alone (no
    free seat anywhere), but one joint swap order lets them exchange seats.
    The order holds both cross-seats while pending, outsiders cannot squeeze
    in, and after both confirm the formal rosters change in a single step.

Storyline B -- disqualification in the middle of a swap
    Another two students open a joint order. One of them confirms, then the
    source school revokes the record behind their enrollment certificate.
    The enrollment stops holding its seat immediately, the whole pending
    order ends as ``disqualified`` (nobody partially moves), the released
    seat becomes available again, and a late confirmation cannot revive it.

Run:
    python examples/demo_training_swap.py --db /tmp/training_demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import training  # noqa: E402
from app.db import connect, reset_db_file  # noqa: E402
from app.models import (  # noqa: E402
    EnrollmentIn,
    IssueIn,
    SwapOrderIn,
    SwapParticipantIn,
    TrainingClassIn,
    TrainingTermIn,
)
from app.seed import (  # noqa: E402
    DEF_LINHAI_BIZ_DATA,
    SCHOOL_LINHAI,
    SCHOOL_XINGHE,
    seed_base_data,
)
from app.services import (  # noqa: E402
    ServiceError,
    create_record,
    create_student,
    issue_certificate,
    revoke_record,
)

TERM_ID = "term_advanced_2026"
DEADLINE = lambda hours=2: datetime.now(timezone.utc) + timedelta(hours=hours)  # noqa: E731


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def capacity_of(conn, class_id: str) -> dict:
    view = training._class_capacity_rows(conn, TERM_ID)
    return view[class_id]


def formal_class(conn, student_id: str) -> str | None:
    view = training.get_student_term_view(conn, TERM_ID, student_id)
    return view["formal_class_id"]


def make_student_with_certificate(conn, index: int) -> tuple[str, str, str]:
    student_id = f"stu_s{index}"
    record_code = f"DEMO-TR-{index}"
    certificate_code = f"CERT-DEMO-{index}"
    create_student(conn, student_id, f"实训学生{index}")
    create_record(conn, record_code, SCHOOL_XINGHE, student_id, "MATH", 4)
    issue_certificate(
        conn,
        IssueIn(
            certificate_code=certificate_code,
            definition_id=DEF_LINHAI_BIZ_DATA,
            student_id=student_id,
            record_codes=[record_code],
        ),
    )
    return student_id, record_code, certificate_code


def enroll(conn, student_id: str, class_id: str, certificate_code: str) -> None:
    training.enroll(
        conn,
        EnrollmentIn(
            term_id=TERM_ID,
            student_id=student_id,
            class_id=class_id,
            certificate_code=certificate_code,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/tmp/training_demo.db")
    args = parser.parse_args()

    reset_db_file(args.db)
    conn = connect(args.db)
    try:
        seed_base_data(conn)

        # Four students, each with their own valid Linhai certificate.
        students = [make_student_with_certificate(conn, i) for i in range(1, 5)]
        s1, rec1, cert1 = students[0]
        s2, _rec2, cert2 = students[1]
        s3, rec3, cert3 = students[2]
        s4, _rec4, cert4 = students[3]

        training.create_term(
            conn,
            TrainingTermIn(
                term_id=TERM_ID,
                organizer_school_id=SCHOOL_LINHAI,
                title="校际进阶实训（联合换班演示）",
                accepted_definition_ids=[DEF_LINHAI_BIZ_DATA],
            ),
        )
        class_alpha = "cls_alpha"
        class_beta = "cls_beta"
        class_gamma = "cls_gamma"
        for class_id, name, cap in [
            (class_alpha, "甲班（容量1）", 1),
            (class_beta, "乙班（容量1）", 1),
            (class_gamma, "丙班（容量2）", 2),
        ]:
            training.create_class(
                conn,
                TrainingClassIn(
                    class_id=class_id, term_id=TERM_ID, name=name, capacity=cap
                ),
            )

        # ------------------------------------------------------------------
        # Storyline A: full-class mutual swap
        # ------------------------------------------------------------------
        print("\n" + "#" * 70)
        print("# 情节 A：两个满班学生互换")
        print("#" * 70)

        enroll(conn, s1, class_alpha, cert1)
        enroll(conn, s2, class_beta, cert2)
        show(
            "A1) 两个容量1的班全部报满，各自可用名额为0",
            {
                class_alpha: capacity_of(conn, class_alpha),
                class_beta: capacity_of(conn, class_beta),
            },
        )
        assert capacity_of(conn, class_alpha)["available"] == 0
        assert capacity_of(conn, class_beta)["available"] == 0

        # A lone move is impossible: nobody leaves a seat behind in that order.
        try:
            training.create_swap_order(
                conn,
                SwapOrderIn(
                    order_id="SW-A-LONE",
                    term_id=TERM_ID,
                    organizer_school_id=SCHOOL_LINHAI,
                    confirm_deadline=DEADLINE(),
                    participants=[SwapParticipantIn(student_id=s1, to_class_id=class_beta)],
                ),
            )
        except ServiceError as exc:
            show(
                "A2) 单人想进已满的乙班：整单无法容纳，被拒绝",
                {"http_status": exc.status_code, "code": exc.code, "message": exc.message},
            )
            assert exc.code == "swap_capacity_exceeded"
        else:  # pragma: no cover
            raise AssertionError("A2 expected swap_capacity_exceeded")

        order_a = training.create_swap_order(
            conn,
            SwapOrderIn(
                order_id="SW-A-MUTUAL",
                term_id=TERM_ID,
                organizer_school_id=SCHOOL_LINHAI,
                confirm_deadline=DEADLINE(),
                participants=[
                    SwapParticipantIn(student_id=s1, to_class_id=class_beta),
                    SwapParticipantIn(student_id=s2, to_class_id=class_alpha),
                ],
            ),
        )
        show(
            "A3) 联合换班单成立：本单转出的名额供本单转入使用，两班净增都是0",
            {
                "order_id": order_a["order_id"],
                "status": order_a["status"],
                "net_class_increases": order_a["net_class_increases"],
                "capacity": {
                    class_alpha: capacity_of(conn, class_alpha),
                    class_beta: capacity_of(conn, class_beta),
                },
            },
        )
        # Both cross-seats are now held by the open order. Each class has one
        # incoming hold and one outgoing hold, so the net reservation is zero
        # and outsiders still see exactly zero available seats.
        assert capacity_of(conn, class_alpha)["held_pending_in"] == 1
        assert capacity_of(conn, class_alpha)["net_pending_reservation"] == 0
        assert capacity_of(conn, class_alpha)["available"] == 0
        assert capacity_of(conn, class_beta)["available"] == 0
        assert formal_class(conn, s1) == class_alpha  # roster unchanged while pending

        training.respond_swap(conn, "SW-A-MUTUAL", s1, "confirmed")
        training.respond_swap(conn, "SW-A-MUTUAL", s2, "confirmed")
        show(
            "A4) 两人全部确认后一次换班：名册同时变更，没有超额",
            {
                "order": training.get_swap_order(conn, "SW-A-MUTUAL")["status"],
                f"{s1}_formal_class": formal_class(conn, s1),
                f"{s2}_formal_class": formal_class(conn, s2),
                "capacity": {
                    class_alpha: capacity_of(conn, class_alpha),
                    class_beta: capacity_of(conn, class_beta),
                },
            },
        )
        assert formal_class(conn, s1) == class_beta
        assert formal_class(conn, s2) == class_alpha
        assert capacity_of(conn, class_alpha)["enrolled"] == 1
        assert capacity_of(conn, class_beta)["enrolled"] == 1

        # ------------------------------------------------------------------
        # Storyline B: disqualification in the middle of a swap
        # ------------------------------------------------------------------
        print("\n" + "#" * 70)
        print("# 情节 B：换班中途证书失效，整单结束、名额释放")
        print("#" * 70)

        enroll(conn, s3, class_gamma, cert3)
        # A balanced mutual swap between beta (s1, after storyline A) and
        # gamma (s3): each class gives up one seat and receives one, so the
        # order as a whole fits even though neither freed seat exists yet.
        order_b = training.create_swap_order(
            conn,
            SwapOrderIn(
                order_id="SW-B-MID",
                term_id=TERM_ID,
                organizer_school_id=SCHOOL_LINHAI,
                confirm_deadline=DEADLINE(),
                participants=[
                    SwapParticipantIn(student_id=s3, to_class_id=class_beta),
                    SwapParticipantIn(student_id=s1, to_class_id=class_gamma),
                ],
            ),
        )
        show(
            "B1) 新的联合换班单成立并留出名额",
            {
                "order_id": order_b["order_id"],
                "participants": [
                    {"student": p["student_id"], "from": p["from_class_id"], "to": p["to_class_id"]}
                    for p in order_b["participants"]
                ],
                "beta_capacity": capacity_of(conn, class_beta),
                "gamma_capacity": capacity_of(conn, class_gamma),
            },
        )

        training.respond_swap(conn, "SW-B-MID", s3, "confirmed")
        show("B2) s3 已确认；s1 尚未确认，名册仍是原班次", {"s1_class": formal_class(conn, s1)})
        assert formal_class(conn, s1) == class_beta

        # The source school revokes the record behind s3's certificate.
        revocation = revoke_record(conn, rec3, SCHOOL_XINGHE, reason="来源凭据复核撤销")
        show(
            "B3) 来源撤销即时生效：s3 的报名不再占位，待确认换班单整单结束",
            {
                "training_effect": revocation["training_effect"],
                "swap_order_status": training.get_swap_order(conn, "SW-B-MID")["status"],
                "s3_formal_class": formal_class(conn, s3),
                "s1_formal_class": formal_class(conn, s1),
                "gamma_capacity": capacity_of(conn, class_gamma),
            },
        )
        assert revocation["training_effect"]["ended_swap_order_ids"] == ["SW-B-MID"]
        assert training.get_swap_order(conn, "SW-B-MID")["status"] == "disqualified"
        assert formal_class(conn, s3) is None
        assert formal_class(conn, s1) == class_beta  # the other student never moved

        # The freed gamma seat is bookable by another student immediately.
        enroll(conn, s4, class_gamma, cert4)
        show(
            "B4) 释放出的名额可被其他学生报名使用",
            {
                f"{s4}_formal_class": formal_class(conn, s4),
                "gamma_capacity": capacity_of(conn, class_gamma),
            },
        )
        assert formal_class(conn, s4) == class_gamma

        # A late confirmation cannot move anyone anymore.
        try:
            training.respond_swap(conn, "SW-B-MID", s1, "confirmed")
        except ServiceError as exc:
            show(
                "B5) 迟到的确认被拒绝：已结束的单不能再次调班",
                {"http_status": exc.status_code, "code": exc.code},
            )
            assert exc.code == "swap_order_closed"
        else:  # pragma: no cover
            raise AssertionError("B5 expected swap_order_closed")
        assert formal_class(conn, s1) == class_beta

        # Organizer roster view reports the same consistent picture.
        roster = training.get_class_roster(
            conn, TERM_ID, class_gamma, SCHOOL_LINHAI
        )
        show(
            "B6) 主办方名册视角：在册学生与剩余名额一致",
            {
                "roster_students": [row["student_id"] for row in roster["roster"]],
                "capacity_view": roster["capacity_view"],
            },
        )
        assert [row["student_id"] for row in roster["roster"]] == [s4]

        print("\nDEMO OK: 满班互换与换班中途失去资格两条时间线均符合规则。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
