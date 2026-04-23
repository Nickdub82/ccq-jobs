"""
Scraper pipeline orchestration — Gmail + Claude edition.

Flow:
    1. Fetch Indeed alert emails from Gmail (last 48h)
    2. For each email, ask Claude to extract individual jobs as JSON
    3. Dedup against DB (by URL or employer+title+location fingerprint)
    4. Insert new jobs into DB (with CCQ preliminary assessment from Claude)
"""
import logging
import sys
from datetime import datetime, timezone

from config import settings
from db import get_session
from models import Job, Employer, Source, JobSource, ScrapingRun
from dedup import make_fingerprint

import gmail_reader
import email_parser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("scraper.run")


def get_or_create_source(db, source_name: str) -> Source:
    src = db.query(Source).filter_by(name=source_name).first()
    if src is None:
        src = Source(
            name=source_name,
            display_name=source_name.title(),
            base_url=f"https://ca.{source_name}.com",
            is_active=True,
        )
        db.add(src)
        db.commit()
        db.refresh(src)
    return src


def get_or_create_employer(db, name: str):
    if not name:
        return None
    normalized = name.lower().strip()
    emp = db.query(Employer).filter_by(normalized_name=normalized).first()
    if emp is None:
        emp = Employer(name=name, normalized_name=normalized)
        db.add(emp)
        db.commit()
        db.refresh(emp)
    return emp


def process_emails(run_id: int) -> dict:
    """Fetch emails, extract jobs via Claude, write to DB."""
    logger.info("Fetching Indeed alert emails from Gmail...")

    try:
        emails = gmail_reader.fetch_indeed_emails(hours_back=48)
    except Exception as e:
        logger.error(f"Gmail fetch failed: {e}", exc_info=True)
        return {"jobs_scraped": 0, "jobs_new": 0, "error": str(e)}

    if not emails:
        logger.info("No Indeed emails found.")
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    # Step 1: Ask Claude to extract jobs from each email
    all_extracted = []
    ai_calls = 0
    for email in emails:
        try:
            jobs = email_parser.extract_jobs_from_email(email)
            ai_calls += 1
            for job in jobs:
                # Attach source info from the email sender
                job["_email_sender"] = email.sender
                job["_email_id"] = email.message_id
                all_extracted.append(job)
        except Exception as e:
            logger.error(f"Failed to extract from email {email.message_id}: {e}", exc_info=True)
            continue

    logger.info(f"Total jobs extracted by Claude: {len(all_extracted)}")

    if not all_extracted:
        return {
            "jobs_scraped": 0,
            "jobs_new": 0,
            "ai_calls": ai_calls,
        }

    # Step 2: Dedup + insert into DB
    db = get_session()
    inserted = 0
    updated = 0
    flagged = 0
    ccq_count = 0

    try:
        # Determine source from sender (indeed/jobillico/jobboom)
        src_indeed = get_or_create_source(db, "indeed")

        for job in all_extracted:
            title = job.get("title")
            employer_name = job.get("employer")
            location = job.get("location")
            original_url = job.get("original_url")

            if not title or not original_url:
                logger.warning(f"Skipping job with missing title/url: {job}")
                continue

            fp = make_fingerprint(employer_name, title, location)

            # Dedup check
            existing = db.query(Job).filter_by(fingerprint=fp).first()
            if existing:
                existing.last_seen_at = datetime.now(timezone.utc)
                db.commit()
                updated += 1
                continue

            # Determine source
            sender_low = (job.get("_email_sender") or "").lower()
            if "indeed" in sender_low:
                src = src_indeed
            else:
                src = src_indeed  # default for now; expand when we add Jobillico/Jobboom

            employer = get_or_create_employer(db, employer_name)

            # CCQ preliminary assessment
            is_likely_ccq = bool(job.get("is_likely_ccq", False))
            ccq_confidence = float(job.get("ccq_confidence", 0) or 0)

            # Auto-approve if CCQ with high confidence, else flag for review
            is_approved = is_likely_ccq and ccq_confidence >= 0.75
            needs_review = not is_approved

            new_job = Job(
                fingerprint=fp,
                external_id=None,  # we could extract from URL if needed
                title=title,
                description=job.get("description"),
                employer_id=employer.id if employer else None,
                location_text=location,
                city=None,
                region=None,
                address=None,
                job_type=None,
                trade="peintre",  # we only subscribe to painter alerts
                salary_text=job.get("salary_text"),
                is_ccq=is_likely_ccq,
                original_url=original_url,
                source_id=src.id,
                ai_confidence=ccq_confidence,
                ai_notes=job.get("notes"),
                is_approved=is_approved,
                needs_review=needs_review,
            )
            db.add(new_job)
            db.commit()
            db.refresh(new_job)

            js = JobSource(
                job_id=new_job.id,
                source_id=src.id,
                source_url=original_url,
            )
            db.add(js)
            db.commit()

            inserted += 1
            if needs_review:
                flagged += 1
            if is_likely_ccq:
                ccq_count += 1

    finally:
        db.close()

    return {
        "jobs_scraped": len(all_extracted),
        "jobs_new": inserted,
        "jobs_updated": updated,
        "jobs_removed": 0,
        "jobs_flagged": flagged,
        "ai_calls": ai_calls,
        "ccq_identified": ccq_count,
    }


def main():
    logger.info("=" * 60)
    logger.info("Scraper starting up (Gmail + Claude edition)")
    logger.info("=" * 60)

    # Smoke-test DB connection
    try:
        db = get_session()
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db.close()
        logger.info("DB connection OK")
    except Exception as e:
        logger.error(f"DB connection failed: {e}", exc_info=True)
        sys.exit(1)

    db = get_session()
    run = ScrapingRun(status="running", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id
    db.close()

    final_stats = {
        "jobs_scraped": 0,
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_removed": 0,
        "jobs_flagged": 0,
        "ai_calls": 0,
    }

    try:
        stats = process_emails(run_id)
        for k, v in stats.items():
            if k in final_stats and isinstance(v, int):
                final_stats[k] += v
    except Exception as e:
        logger.error(f"Run failed: {e}", exc_info=True)

    db = get_session()
    run = db.query(ScrapingRun).get(run_id)
    run.finished_at = datetime.now(timezone.utc)
    run.status = "success"
    run.jobs_scraped = final_stats["jobs_scraped"]
    run.jobs_new = final_stats["jobs_new"]
    run.jobs_updated = final_stats["jobs_updated"]
    run.jobs_removed = final_stats["jobs_removed"]
    run.jobs_flagged = final_stats["jobs_flagged"]
    run.ai_calls = final_stats["ai_calls"]
    db.commit()
    db.close()

    logger.info(f"Run {run_id} complete. Final stats: {final_stats}")


if __name__ == "__main__":
    main()
