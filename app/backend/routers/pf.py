from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import pf_runner

router = APIRouter()


class RunRequest(BaseModel):
    start: datetime = Field(..., description="ISO datetime (e.g. 2025-04-15T00:00:00)")
    end: datetime = Field(..., description="ISO datetime (inclusive last hour)")
    mode: Literal["lopf", "pf"] = "lopf"
    aggregate: bool = Field(True,
        description="Group generators by (bus, carrier) before solving — required "
                    "for grid_beta to be tractable (18792 → ~few hundred LP vars).")


@router.post("/run")
def run(req: RunRequest):
    if req.end < req.start:
        raise HTTPException(400, "end must be >= start")
    try:
        job_id = pf_runner.submit(req.start, req.end, req.mode, aggregate=req.aggregate)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "status_url": f"/api/pf/status/{job_id}"}


@router.get("/status/{job_id}")
def status(job_id: str):
    s = pf_runner.status(job_id)
    if s is None:
        raise HTTPException(404, f"Job {job_id} not found")
    return s


@router.get("/result/{job_id}")
def result(job_id: str):
    s = pf_runner.status(job_id)
    if s is not None and s["state"] == "running":
        raise HTTPException(409, "Job still running; poll /status first")
    res = pf_runner.result(job_id)
    if res is None:
        raise HTTPException(404, f"No result for job {job_id}")
    return res


@router.get("/jobs")
def list_jobs():
    return pf_runner.list_jobs()
