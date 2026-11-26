"""FastAPI application and HTTP routes."""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import equipment, services, training
from .db import connect, init_db
from .models import (
    ConfirmRevocationIn,
    EquipmentCancelIn,
    EquipmentItemIn,
    EquipmentLiftIn,
    EquipmentPickupIn,
    EquipmentReservationIn,
    EquipmentRescheduleIn,
    EquipmentReturnIn,
    EquipmentSuspensionIn,
    EquipmentTagsIn,
    DefinitionIn,
    EnrollmentIn,
    IssueIn,
    RecordIn,
    ReviewIn,
    RevocationIn,
    SchoolIn,
    StudentIn,
    SwapOrderIn,
    SwapResponseIn,
    TrainingClassIn,
    TrainingTermIn,
    WithdrawalIn,
)
from .services import ServiceError


def create_app(db_path: Optional[str] = None) -> FastAPI:
    resolved_db_path = db_path or os.environ.get("MICROCERT_DB") or "microcred.db"
    init_db(resolved_db_path)

    app = FastAPI(
        title="校际微证书学分互认后端",
        version="1.0.0",
        description=(
            "本地学校、学生和正式修读记录管理；逐项匹配、预审、原子颁证、"
            "来源撤销、待处理占用与确认释放。"
        ),
    )
    app.state.db_path = resolved_db_path

    def get_db(request: Request):
        conn = connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    DbConn = Depends(get_db)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "database": resolved_db_path}

    router = APIRouter(prefix="/api", tags=["local-ledger"])

    # ----- schools and students -------------------------------------------

    @router.post("/schools", status_code=201)
    def create_school(payload: SchoolIn, conn: sqlite3.Connection = DbConn):
        return services.create_school(conn, payload.school_id, payload.name)

    @router.get("/schools")
    def list_schools(conn: sqlite3.Connection = DbConn):
        return {"schools": services.list_schools(conn)}

    @router.get("/schools/{school_id}")
    def get_school(school_id: str, conn: sqlite3.Connection = DbConn):
        return services.get_school(conn, school_id)

    @router.get("/schools/{school_id}/ledger")
    def get_school_ledger(school_id: str, conn: sqlite3.Connection = DbConn):
        return services.get_school_ledger(conn, school_id)

    @router.get("/schools/{school_id}/certificates")
    def list_school_certificates(
        school_id: str,
        status: Optional[str] = Query(
            default=None,
            description="active | revocation_pending | revocation_processed",
        ),
        conn: sqlite3.Connection = DbConn,
    ):
        return services.list_school_certificates(conn, school_id, status)

    @router.post("/students", status_code=201)
    def create_student(payload: StudentIn, conn: sqlite3.Connection = DbConn):
        return services.create_student(conn, payload.student_id, payload.name)

    @router.get("/students/{student_id}")
    def get_student(student_id: str, conn: sqlite3.Connection = DbConn):
        student = services.get_student(conn, student_id)
        student["records"] = services.list_records(conn, student_id=student_id)
        return student

    @router.get("/students/{student_id}/certificates")
    def list_student_certificates(student_id: str, conn: sqlite3.Connection = DbConn):
        return services.list_student_certificates(conn, student_id)

    # ----- source records --------------------------------------------------

    @router.post("/records", status_code=201)
    def publish_record(payload: RecordIn, conn: sqlite3.Connection = DbConn):
        return services.create_record(
            conn,
            record_code=payload.record_code,
            source_school_id=payload.source_school_id,
            student_id=payload.student_id,
            category=payload.category,
            points=payload.points,
        )

    @router.get("/records")
    def list_records(
        student_id: Optional[str] = None,
        source_school_id: Optional[str] = None,
        conn: sqlite3.Connection = DbConn,
    ):
        return {
            "records": services.list_records(
                conn, student_id=student_id, source_school_id=source_school_id
            )
        }

    @router.get("/records/{record_code}")
    def get_record(record_code: str, conn: sqlite3.Connection = DbConn):
        return services.get_record(conn, record_code)

    @router.get("/records/{record_code}/usage")
    def get_record_usage(record_code: str, conn: sqlite3.Connection = DbConn):
        return services.get_record_usage(conn, record_code)

    @router.post("/records/{record_code}/revoke")
    def revoke_record(
        record_code: str,
        payload: RevocationIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return services.revoke_record(
            conn,
            record_code=record_code,
            source_school_id=payload.source_school_id,
            reason=payload.reason,
        )

    # ----- credential definitions -----------------------------------------

    @router.post("/credential-definitions", status_code=201)
    def create_definition(payload: DefinitionIn, conn: sqlite3.Connection = DbConn):
        return services.create_definition(conn, payload)

    @router.get("/credential-definitions")
    def list_definitions(conn: sqlite3.Connection = DbConn):
        return {"definitions": services.list_definitions(conn)}

    @router.get("/credential-definitions/{definition_id}")
    def get_definition(definition_id: str, conn: sqlite3.Connection = DbConn):
        return services.get_definition(conn, definition_id)

    # ----- review and issuance --------------------------------------------

    @router.post("/reviews")
    def review(payload: ReviewIn, conn: sqlite3.Connection = DbConn):
        return services.review_definition(
            conn,
            definition_id=payload.definition_id,
            student_id=payload.student_id,
            record_codes=payload.record_codes,
        )

    @router.post("/certificates/issue", status_code=201)
    def issue_certificate(payload: IssueIn, conn: sqlite3.Connection = DbConn):
        return services.issue_certificate(conn, payload)

    @router.get("/certificates/{certificate_code}")
    def get_certificate(certificate_code: str, conn: sqlite3.Connection = DbConn):
        return services.get_certificate_detail(conn, certificate_code)

    @router.post("/certificates/{certificate_code}/confirm-revocation")
    def confirm_revocation(
        certificate_code: str,
        payload: ConfirmRevocationIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return services.confirm_certificate_revocation(
            conn,
            certificate_code=certificate_code,
            issuer_school_id=payload.issuer_school_id,
        )

    app.include_router(router)

    # ----- advanced practical training ------------------------------------
    training_router = APIRouter(prefix="/api/training", tags=["training"])

    @training_router.post("/terms", status_code=201)
    def create_term(payload: TrainingTermIn, conn: sqlite3.Connection = DbConn):
        return training.create_term(conn, payload)

    @training_router.get("/terms")
    def list_terms(conn: sqlite3.Connection = DbConn):
        return training.list_terms(conn)

    @training_router.get("/terms/{term_id}")
    def get_term(term_id: str, conn: sqlite3.Connection = DbConn):
        return training.get_term(conn, term_id)

    @training_router.post("/classes", status_code=201)
    def create_class(payload: TrainingClassIn, conn: sqlite3.Connection = DbConn):
        return training.create_class(conn, payload)

    @training_router.get("/terms/{term_id}/classes")
    def list_classes(term_id: str, conn: sqlite3.Connection = DbConn):
        return training.list_classes(conn, term_id)

    @training_router.get("/terms/{term_id}/classes/{class_id}/roster")
    def get_class_roster(
        term_id: str,
        class_id: str,
        organizer_school_id: str = Query(
            ..., description="the organizing school's local school_id"
        ),
        conn: sqlite3.Connection = DbConn,
    ):
        return training.get_class_roster(conn, term_id, class_id, organizer_school_id)

    @training_router.post("/enrollments", status_code=201)
    def enroll(payload: EnrollmentIn, conn: sqlite3.Connection = DbConn):
        return training.enroll(conn, payload)

    @training_router.post("/withdrawals")
    def withdraw(payload: WithdrawalIn, conn: sqlite3.Connection = DbConn):
        return training.withdraw(conn, payload.term_id, payload.student_id)

    @training_router.get("/terms/{term_id}/students/{student_id}")
    def get_student_term_view(
        term_id: str, student_id: str, conn: sqlite3.Connection = DbConn
    ):
        return training.get_student_term_view(conn, term_id, student_id)

    @training_router.post("/swap-orders", status_code=201)
    def create_swap_order(payload: SwapOrderIn, conn: sqlite3.Connection = DbConn):
        return training.create_swap_order(conn, payload)

    @training_router.get("/swap-orders")
    def list_swap_orders(
        term_id: Optional[str] = None,
        status: Optional[str] = Query(
            default=None,
            description=(
                "pending_confirmation | swapped | rejected | withdrawn | "
                "disqualified | expired"
            ),
        ),
        conn: sqlite3.Connection = DbConn,
    ):
        return training.list_swap_orders(conn, term_id=term_id, status=status)

    @training_router.get("/swap-orders/{order_id}")
    def get_swap_order(order_id: str, conn: sqlite3.Connection = DbConn):
        return training.get_swap_order(conn, order_id)

    @training_router.post("/swap-orders/{order_id}/responses")
    def respond_swap(
        order_id: str,
        payload: SwapResponseIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return training.respond_swap(
            conn,
            order_id=order_id,
            student_id=payload.student_id,
            response=payload.response,
        )

    app.include_router(training_router)

    # ----- shared equipment room ------------------------------------------

    equipment_router = APIRouter(prefix="/api/equipment", tags=["equipment"])

    @equipment_router.post("/items", status_code=201)
    def create_equipment_item(
        payload: EquipmentItemIn, conn: sqlite3.Connection = DbConn
    ):
        return equipment.create_item(conn, payload)

    @equipment_router.get("/items")
    def list_equipment_items(
        owner_school_id: Optional[str] = None,
        tag: Optional[str] = None,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.list_items(conn, owner_school_id=owner_school_id, tag=tag)

    @equipment_router.get("/items/{item_id}")
    def get_equipment_item(item_id: str, conn: sqlite3.Connection = DbConn):
        return equipment.get_item(conn, item_id)

    @equipment_router.post("/items/{item_id}/tags")
    def set_equipment_item_tags(
        item_id: str, payload: EquipmentTagsIn, conn: sqlite3.Connection = DbConn
    ):
        return equipment.set_item_tags(conn, item_id, payload)

    @equipment_router.post("/items/{item_id}/suspensions", status_code=201)
    def suspend_equipment_item(
        item_id: str, payload: EquipmentSuspensionIn, conn: sqlite3.Connection = DbConn
    ):
        return equipment.add_suspension(conn, item_id, payload)

    @equipment_router.post(
        "/items/{item_id}/suspensions/{suspension_id}/lift"
    )
    def lift_equipment_suspension(
        item_id: str,
        suspension_id: int,
        payload: EquipmentLiftIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.lift_suspension(
            conn, item_id, suspension_id, payload.owner_school_id
        )

    @equipment_router.post("/reservations", status_code=201)
    def create_equipment_reservation(
        payload: EquipmentReservationIn, conn: sqlite3.Connection = DbConn
    ):
        return equipment.create_reservation(conn, payload)

    @equipment_router.get("/reservations")
    def list_equipment_reservations(
        organizer_school_id: Optional[str] = None,
        term_id: Optional[str] = None,
        status: Optional[str] = Query(
            default=None,
            description="accepted | picked_up | returned | cancelled | expired",
        ),
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.list_reservations(
            conn,
            organizer_school_id=organizer_school_id,
            term_id=term_id,
            status=status,
        )

    @equipment_router.get("/reservations/{reservation_id}")
    def get_equipment_reservation(
        reservation_id: str,
        organizer_school_id: Optional[str] = None,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.get_reservation(
            conn, reservation_id, organizer_school_id=organizer_school_id
        )

    @equipment_router.post("/reservations/{reservation_id}/reschedule")
    def reschedule_equipment_reservation(
        reservation_id: str,
        payload: EquipmentRescheduleIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.reschedule_reservation(
            conn,
            reservation_id,
            payload.organizer_school_id,
            payload.start_at,
            payload.end_at,
        )

    @equipment_router.post("/reservations/{reservation_id}/cancel")
    def cancel_equipment_reservation(
        reservation_id: str,
        payload: EquipmentCancelIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.cancel_reservation(
            conn, reservation_id, payload.organizer_school_id
        )

    @equipment_router.post("/reservations/{reservation_id}/pickup")
    def pickup_equipment_reservation(
        reservation_id: str,
        payload: EquipmentPickupIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.pickup(conn, reservation_id, payload)

    @equipment_router.post("/reservations/{reservation_id}/return")
    def return_equipment_reservation(
        reservation_id: str,
        payload: EquipmentReturnIn,
        conn: sqlite3.Connection = DbConn,
    ):
        return equipment.return_items(
            conn, reservation_id, payload.organizer_school_id
        )

    @equipment_router.get("/schools/{school_id}/board")
    def equipment_school_board(school_id: str, conn: sqlite3.Connection = DbConn):
        return equipment.school_board(conn, school_id)

    app.include_router(equipment_router)

    # Public verification is intentionally not under an administrative noun.
    @app.get("/verify/{certificate_code}", tags=["verification"])
    def verify(certificate_code: str, conn: sqlite3.Connection = DbConn):
        return services.verify_certificate(conn, certificate_code)

    @app.get("/", tags=["system"])
    def root():
        return {
            "name": "校际微证书后端",
            "docs": "/docs",
            "health": "/health",
            "verify": "/verify/{certificate_code}",
        }

    return app


app = create_app()
