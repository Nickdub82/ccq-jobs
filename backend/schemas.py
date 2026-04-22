"""Pydantic schemas for API request/response."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    display_name: str


class EmployerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    website: Optional[str] = None


class JobSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    source_url: str
    source: SourceOut


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: Optional[str] = None
    employer: Optional[EmployerOut] = None

    location_text: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    job_type: Optional[str] = None
    trade: Optional[str] = None
    salary_text: Optional[str] = None
    is_ccq: bool = False

    original_url: str
    source: Optional[SourceOut] = None
    posted_at: Optional[datetime] = None
    first_seen_at: datetime
    last_seen_at: datetime

    ai_confidence: Optional[float] = None
    ai_notes: Optional[str] = None
    needs_review: bool = False

    job_sources: list[JobSourceOut] = []


class JobListResponse(BaseModel):
    total: int
    items: list[JobOut]


class ScrapingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_id: Optional[int] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    jobs_scraped: int
    jobs_new: int
    jobs_updated: int
    jobs_removed: int
    jobs_flagged: int
    ai_calls: int
    error_message: Optional[str] = None


class ReviewDecision(BaseModel):
    approve: bool  # True = approve, False = reject (delete)
    notes: Optional[str] = None
