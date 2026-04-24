"""Admin endpoints: review queue + approved jobs management + scraping logs."""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc

from db import get_db
from config import settings
from models import Job, ScrapingRun, JobSource
from schemas import JobOut, JobListResponse, ScrapingRunOut, ReviewDecision

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(x_admin_password: str = Header(None)):
    if not x_admin_password or x_admin_password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


@router.get("/review-queue", response_model=JobListResponse, dependencies=[Depends(require_admin)])
def review_queue(db: Session = Depends(get_db)):
    """All jobs Claude flagged as uncertain."""
    q = (
        db.query(Job)
        .options(joinedload(Job.employer), joinedload(Job.source), joinedload(Job.job_sources))
        .filter(Job.needs_review == True)
        .order_by(desc(Job.first_seen_at))
    )
    items = q.all()
    return {"total": len(items), "items": items}


@router.get("/approved", response_model=JobListResponse, dependencies=[Depends(require_admin)])
def approved_jobs(db: Session = Depends(get_db)):
    """All currently-approved (publicly visible) jobs, for admin management."""
    q = (
        db.query(Job)
        .options(joinedload(Job.employer), joinedload(Job.source), joinedload(Job.job_sources))
        .filter(Job.is_approved == True)
        .order_by(desc(Job.first_seen_at))
    )
    items = q.all()
    return {"total": len(items), "items": items}


@router.post("/review/{job_id}", dependencies=[Depends(require_admin)])
def review_job(job_id: int, decision: ReviewDecision, db: Session = Depends(get_db)):
    """Approve or reject a flagged job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if decision.approve:
        job.is_approved = True
        job.needs_review = False
        if decision.notes:
            job.ai_notes = (job.ai_notes or "") + f"\n[Admin] {decision.notes}"
        db.commit()
        return {"status": "approved", "job_id": job_id}
    else:
        # Clean up job_sources first (FK constraint)
        db.query(JobSource).filter(JobSource.job_id == job_id).delete()
        db.delete(job)
        db.commit()
        return {"status": "deleted", "job_id": job_id}


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_admin)])
def delete_job(job_id: int, db: Session = Depends(get_db)):
    """
    Hard-delete any job (approved, review, etc.). Used when the admin
    spots a bad job on the public portal (e.g., wrong trade, false positive).
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Remove job_sources links first (FK constraint)
    db.query(JobSource).filter(JobSource.job_id == job_id).delete()
    db.delete(job)
    db.commit()
    return {"status": "deleted", "job_id": job_id}


@router.get("/runs", response_model=list[ScrapingRunOut], dependencies=[Depends(require_admin)])
def list_runs(limit: int = 50, db: Session = Depends(get_db)):
    """Recent scraping runs for debugging."""
    return (
        db.query(ScrapingRun)
        .order_by(desc(ScrapingRun.started_at))
        .limit(limit)
        .all()
    )


@router.get("/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db)):
    total = db.query(Job).count()
    approved = db.query(Job).filter(Job.is_approved == True).count()
    review = db.query(Job).filter(Job.needs_review == True).count()
    ccq = db.query(Job).filter(Job.is_ccq == True, Job.is_approved == True).count()
    return {
        "total_jobs": total,
        "approved": approved,
        "in_review": review,
        "ccq_confirmed": ccq,
    }
