"""Public job listing endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, and_, desc

from db import get_db
from models import Job
from schemas import JobOut, JobListResponse

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=JobListResponse)
def list_jobs(
    region: Optional[str] = Query(None, description="e.g. 'Montreal'"),
    trade: Optional[str] = Query(None, description="e.g. 'peintre'"),
    ccq_only: bool = Query(True, description="Only CCQ-confirmed jobs"),
    search: Optional[str] = Query(None, description="Text search in title + description"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List jobs with filters. Only returns approved (non-review-queue) jobs."""
    q = (
        db.query(Job)
        .options(
            joinedload(Job.employer),
            joinedload(Job.source),
            joinedload(Job.job_sources),
        )
        .filter(Job.is_approved == True)
        .filter(Job.needs_review == False)
    )

    if ccq_only:
        q = q.filter(Job.is_ccq == True)
    if region:
        q = q.filter(Job.region.ilike(f"%{region}%"))
    if trade:
        q = q.filter(Job.trade.ilike(f"%{trade}%"))
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Job.title.ilike(like), Job.description.ilike(like)))

    total = q.count()
    items = q.order_by(desc(Job.posted_at), desc(Job.first_seen_at)).offset(offset).limit(limit).all()

    return {"total": total, "items": items}


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a single job by ID."""
    job = (
        db.query(Job)
        .options(
            joinedload(Job.employer),
            joinedload(Job.source),
            joinedload(Job.job_sources),
        )
        .filter(Job.id == job_id, Job.is_approved == True)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/map/pins", response_model=list[JobOut])
def map_pins(
    region: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Return approved jobs that have coordinates, for the map view."""
    q = (
        db.query(Job)
        .options(joinedload(Job.employer), joinedload(Job.source))
        .filter(
            Job.is_approved == True,
            Job.needs_review == False,
            Job.latitude.isnot(None),
            Job.longitude.isnot(None),
        )
    )
    if region:
        q = q.filter(Job.region.ilike(f"%{region}%"))
    return q.limit(500).all()
