import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import JOB_QUEUE_KEY

router = APIRouter()

ALLOWED_JOB_TYPES = {"chat"}


class JobCreate(BaseModel):
    type: str
    payload: dict[str, Any]


class JobCreated(BaseModel):
    id: str
    status: str


class JobStatus(BaseModel):
    id: str
    type: str
    status: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(req: JobCreate, request: Request) -> JobCreated:
    if req.type not in ALLOWED_JOB_TYPES:
        raise HTTPException(status_code=422, detail=f"unknown job type: {req.type}")

    job_id = str(uuid.uuid4())
    async with request.app.state.pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs (id, type, status, payload)
            VALUES ($1, $2, 'pending', $3::jsonb)
            """,
            job_id,
            req.type,
            json.dumps(req.payload),
        )
    await request.app.state.redis.rpush(JOB_QUEUE_KEY, job_id)
    return JobCreated(id=job_id, status="pending")


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, request: Request) -> JobStatus:
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, type, status, payload, result, error FROM jobs WHERE id = $1",
            job_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")

    return JobStatus(
        id=str(row["id"]),
        type=row["type"],
        status=row["status"],
        payload=json.loads(row["payload"]),
        result=json.loads(row["result"]) if row["result"] else None,
        error=row["error"],
    )
