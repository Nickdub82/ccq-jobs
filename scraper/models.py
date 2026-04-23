"""SQLAlchemy ORM models — self-contained copy for the scraper.

Mirrors backend/models.py but imports Base from scraper/db.py.
Keeping them in sync is manual; they should match db/schema.sql.
"""
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Float,
    ForeignKey, Numeric, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db import Base


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(200), nullable=False)
    base_url = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Employer(Base):
    __tablename__ = "employers"

    id = Column(Integer, primary_key=True)
    name = Column(String(300), nullable=False)
    normalized_name = Column(String(300), nullable=False, unique=True)
    website = Column(String(500))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    jobs = relationship("Job", back_populates="employer")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(64), nullable=False, unique=True)
    external_id = Column(String(200))

    title = Column(String(500), nullable=False)
    description = Column(Text)
    employer_id = Column(Integer, ForeignKey("employers.id", ondelete="SET NULL"))

    location_text = Column(String(300))
    city = Column(String(100))
    region = Column(String(100))
    address = Column(String(500))
    latitude = Column(Float)
    longitude = Column(Float)

    job_type = Column(String(50))
    trade = Column(String(100))
    salary_text = Column(String(200))
    is_ccq = Column(Boolean, default=False)

    original_url = Column(String(1000), nullable=False)
    source_id = Column(Integer, ForeignKey("sources.id"))
    posted_at = Column(DateTime(timezone=True))
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    ai_confidence = Column(Float)
    ai_notes = Column(Text)
    is_approved = Column(Boolean, default=False)
    needs_review = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    employer = relationship("Employer", back_populates="jobs")
    source = relationship("Source")
    job_sources = relationship("JobSource", back_populates="job", cascade="all, delete-orphan")


class JobSource(Base):
    __tablename__ = "job_sources"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=False)
    source_url = Column(String(1000), nullable=False)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("job_id", "source_id", name="uq_job_source"),)

    job = relationship("Job", back_populates="job_sources")
    source = relationship("Source")


class ScrapingRun(Base):
    __tablename__ = "scraping_runs"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("sources.id"))
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    status = Column(String(20), default="running")
    jobs_scraped = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    jobs_updated = Column(Integer, default=0)
    jobs_removed = Column(Integer, default=0)
    jobs_flagged = Column(Integer, default=0)
    ai_calls = Column(Integer, default=0)
    ai_cost_estimate = Column(Numeric(10, 4))
    error_message = Column(Text)
    notes = Column(Text)
