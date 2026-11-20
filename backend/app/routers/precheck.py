from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import precheck_service as svc

router = APIRouter(tags=["precheck"])


class PrecheckIn(BaseModel):
    dock_day: str


@router.post("/precheck/run")
def run(body: PrecheckIn):
    conn = connect()
    try:
        return svc.run_precheck(conn, body.dock_day)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()


@router.get("/precheck/batches")
def batches(dock_day: str | None = None):
    conn = connect()
    try:
        return svc.list_prechecks(conn, dock_day)
    finally:
        conn.close()


@router.get("/precheck/batches/{precheck_id}")
def detail(precheck_id: int):
    conn = connect()
    try:
        return svc.get_precheck(conn, precheck_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    finally:
        conn.close()
