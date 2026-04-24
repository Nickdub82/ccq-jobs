"""
Scraper pipeline orchestration — Gmail + Web Search + Claude edition (v3).

v3 changes:
- Added Volet 2: web search via Serper for off-board CCQ jobs
  (employer career pages, smaller sites not covered by Gmail)
- Both volets feed into the same Claude extractor + DB pipeline
- Dedup via fingerprint means duplicates across volets are handled automatically
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
import web_search

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
            base_url=f"https://{source_name}.com",
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


def decide_job_status(is_likely_ccq: bool, ccq_confidence: float, claude_needs_review: bool):
    if not is_likely_ccq and ccq_confidence >= 0.80:
        return ("skip", False, False)
    if claude_needs_review:
        return ("save_review", False, True)
    if is_likely_ccq and ccq_confidence >= 0.75:
        return ("save_approved", True, False)
    return ("save_review", False, True)


def save_extracted_jobs(db, extracted_jobs: list[dict], default_source_name: str) -> dict:
    src_default = get_or_create_source(db, default_source_name)

    inserted_approved = 0
    inserted_review = 0
    skipped_non_ccq = 0
    updated = 0
    ccq_count = 0

    for job in extracted_jobs:
        title = job.get("title")
        employer_name = job.get("employer")
        location = job.get("location")
        original_url = job.get("original_url")

        if not title or not original_url:
            logger.warning(f"Skipping job with missing title/url: {title}")
            continue

        is_likely_ccq = bool(job.get("is_likely_ccq", False))
        ccq_confidence = float(job.get("ccq_confidence", 0) or 0)
        claude_needs_review = bool(job.get("needs_review", False))

        action, is_approved, needs_review = decide_job_status(
            is_likely_ccq, ccq_confidence, claude_needs_review
        )

        if action == "skip":
            logger.info(f"Skipping non-CCQ job: {title} (conf: {ccq_confidence})")
            skipped_non_ccq += 1
            continue

        fp = make_fingerprint(employer_name, title, location)

        existing = db.query(Job).filter_by(fingerprint=fp).first()
        if existing:
            existing.last_seen_at = datetime.now(timezone.utc)
            db.commit()
            updated += 1
            continue

        claude_source = job.get("source") or default_source_name
        if claude_source != default_source_name:
            src = get_or_create_source(db, claude_source)
        else:
            src = src_default

        employer = get_or_create_employer(db, employer_name)

        new_job = Job(
            fingerprint=fp,
            external_id=None,
            title=title,
            description=job.get("description"),
            employer_id=employer.id if employer else None,
            location_text=location,
            city=None,
            region=None,
            address=None,
            job_type=None,
            trade="peintre",
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

        if action == "save_approved":
            inserted_approved += 1
        else:
            inserted_review += 1

        if is_likely_ccq:
            ccq_count += 1

    return {
        "inserted_approved": inserted_approved,
        "inserted_review": inserted_review,
        "skipped_non_ccq": skipped_non_ccq,
        "updated": updated,
        "ccq_count": ccq_count,
    }


def process_volet_1_gmail(run_id: int) -> dict:
    """Volet 1: Gmail inbox alerts (Indeed, Glassdoor, etc.)."""
    logger.info("-" * 60)
    logger.info("VOLET 1: Gmail inbox emails")
    logger.info("-" * 60)

    try:
        emails = gmail_reader.fetch_all_inbox_emails(hours_back=48)
    except Exception as e:
        logger.error(f"Gmail fetch failed: {e}", exc_info=True)
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    if not emails:
        logger.info("No inbox emails found.")
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    all_extracted = []
    ai_calls = 0
    for email in emails:
        try:
            jobs = email_parser.extract_jobs_from_email(email)
            ai_calls += 1
            for job in jobs:
                all_extracted.append(job)
        except Exception as e:
            logger.error(f"Failed to extract from email {email.message_id}: {e}", exc_info=True)
            continue

    logger.info(f"Volet 1 extracted {len(all_extracted)} jobs from {len(emails)} emails.")

    if not all_extracted:
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": ai_calls}

    db = get_session()
    try:
        stats = save_extracted_jobs(db, all_extracted, "indeed")
    finally:
        db.close()

    logger.info(
        f"Volet 1 DB writes: {stats['inserted_approved']} approved, "
        f"{stats['inserted_review']} review, {stats['skipped_non_ccq']} skipped, "
        f"{stats['updated']} already in DB"
    )

    return {
        "jobs_scraped": len(all_extracted),
        "jobs_new": stats["inserted_approved"] + stats["inserted_review"],
        "jobs_updated": stats["updated"],
        "jobs_flagged": stats["inserted_review"],
        "ai_calls": ai_calls,
        "skipped_non_ccq": stats["skipped_non_ccq"],
    }


def process_volet_2_websearch(run_id: int) -> dict:
    """Volet 2: Serper web search for off-board CCQ jobs."""
    logger.info("-" * 60)
    logger.info("VOLET 2: Web search (Serper)")
    logger.info("-" * 60)

    try:
        pages = web_search.find_ccq_job_pages(max_results_per_query=10)
    except Exception as e:
        logger.error(f"Web search failed: {e}", exc_info=True)
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    if not pages:
        logger.info("No pages found from web search.")
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    all_extracted = []
    ai_calls = 0
    for page in pages:
        try:
            jobs = email_parser.extract_jobs_from_email(page)
            ai_calls += 1
            for job in jobs:
                if not job.get("original_url"):
                    job["original_url"] = page.message_id
                all_extracted.append(job)
        except Exception as e:
            logger.error(f"Failed to extract from page {page.message_id}: {e}", exc_info=True)
            continue

    logger.info(f"Volet 2 extracted {len(all_extracted)} jobs from {len(pages)} pages.")

    if not all_extracted:
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": ai_calls}

    db = get_session()
    try:
        stats = save_extracted_jobs(db, all_extracted, "web")
    finally:
        db.close()

    logger.info(
        f"Volet 2 DB writes: {stats['inserted_approved']} approved, "
        f"{stats['inserted_review']} review, {stats['skipped_non_ccq']} skipped, "
        f"{stats['updated']} already in DB"
    )

    return {
        "jobs_scraped": len(all_extracted),
        "jobs_new": stats["inserted_approved"] + stats["inserted_review"],
        "jobs_updated": stats["updated"],
        "jobs_flagged": stats["inserted_review"],
        "ai_calls": ai_calls,
        "skipped_non_ccq": stats["skipped_non_ccq"],
    }


def main():
    logger.info("=" * 60)
    logger.info("Scraper starting up (Gmail + Web Search + Claude v3)")
    logger.info("=" * 60)

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
        v1_stats = process_volet_1_gmail(run_id)
        for k, v in v1_stats.items():
            if k in final_stats and isinstance(v, int):
                final_stats[k] += v
    except Exception as e:
        logger.error(f"Volet 1 failed: {e}", exc_info=True)

    try:
        v2_stats = process_volet_2_websearch(run_id)
        for k, v in v2_stats.items():
            if k in final_stats and isinstance(v, int):
                final_stats[k] += v
    except Exception as e:
        logger.error(f"Volet 2 failed: {e}", exc_info=True)

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

    logger.info("=" * 60)
    logger.info(f"Run {run_id} complete. Final stats: {final_stats}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
