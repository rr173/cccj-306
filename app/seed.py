"""Seed the readable three-school ledger used by README and the demo script."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Union

from .db import connect, init_db, reset_db_file
from .models import DefinitionIn, RequirementIn
from .services import create_definition, create_record, create_school, create_student

SCHOOL_XINGHE = "sch_xinghe"
SCHOOL_YUNSONG = "sch_yunsong"
SCHOOL_LINHAI = "sch_linhai"
STUDENT_LIN = "stu_lin"

DEF_YUNSONG_DIGITAL = "def_yunsong_digital"
DEF_LINHAI_BIZ_DATA = "def_linhai_biz_data"

RECORD_VERSATILE = "XH-2026-1001"
RECORD_CS_REVOKED = "XH-2026-1002"
RECORD_STAT_BACKUP = "XH-2026-1003"


def seed_base_data(conn) -> None:
    """Insert the three schools, one student, three records and two credentials."""
    create_school(conn, SCHOOL_XINGHE, "星河理工学院（来源学校）")
    create_school(conn, SCHOOL_YUNSONG, "云杉数字学院（颁证学校）")
    create_school(conn, SCHOOL_LINHAI, "临海商学院（另一所颁证学校）")

    create_student(conn, STUDENT_LIN, "林知夏")

    # MATH-4 qualifies for both Yunsong requirement items but can be used once.
    create_record(
        conn,
        RECORD_VERSATILE,
        SCHOOL_XINGHE,
        STUDENT_LIN,
        "MATH",
        4,
    )
    create_record(
        conn,
        RECORD_CS_REVOKED,
        SCHOOL_XINGHE,
        STUDENT_LIN,
        "CS",
        3,
    )
    create_record(
        conn,
        RECORD_STAT_BACKUP,
        SCHOOL_XINGHE,
        STUDENT_LIN,
        "STAT",
        5,
    )

    create_definition(
        conn,
        DefinitionIn(
            definition_id=DEF_YUNSONG_DIGITAL,
            issuer_school_id=SCHOOL_YUNSONG,
            title="云杉数字素养微证书",
            requirements=[
                RequirementIn(
                    requirement_code="logic",
                    name="计算/数学基础",
                    accepted_categories=["CS", "MATH"],
                    min_points=3,
                ),
                RequirementIn(
                    requirement_code="quant",
                    name="量化分析基础",
                    accepted_categories=["MATH", "STAT"],
                    min_points=3,
                ),
            ],
        ),
    )

    create_definition(
        conn,
        DefinitionIn(
            definition_id=DEF_LINHAI_BIZ_DATA,
            issuer_school_id=SCHOOL_LINHAI,
            title="临海商务数据微证书",
            requirements=[
                RequirementIn(
                    requirement_code="math_core",
                    name="数学核心课",
                    accepted_categories=["MATH"],
                    min_points=4,
                )
            ],
        ),
    )


def seed_database(db_path: Union[str, Path], reset: bool = False) -> None:
    if reset:
        reset_db_file(db_path)
    else:
        init_db(db_path)
    conn = connect(db_path)
    try:
        seed_base_data(conn)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the three-school example ledger")
    parser.add_argument("--db", default="microcred_demo.db", help="SQLite database path")
    parser.add_argument("--reset", action="store_true", help="delete the DB file first")
    args = parser.parse_args()
    seed_database(args.db, reset=args.reset)
    print(f"seeded three-school ledger at {args.db}")


if __name__ == "__main__":
    main()
