"""FastAPI application and HTTP routes."""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import services
from .db import connect, init_db
from .models import (
    ConfirmRevocationIn,
    DefinitionIn,
    IssueIn,
    RecordIn,
    ReviewIn,
    RevocationIn,
    SchoolIn,
    StudentIn,
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
