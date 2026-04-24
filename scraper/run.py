"""
Scraper pipeline orchestration — Gmail + Web Search + Claude (v4).

v4 adds SOURCE CACHING:
- Each email (by Gmail message_id) is processed ONCE, ever
- Each web page (by URL) is processed ONCE, ever
- Subsequent runs skip them -> no Claude tokens wasted
- Saves ~$15-30/month and makes runs way faster after the first one
"""
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text as sql_text

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


# ============================================================
# PROCESSED SOURCES CACHE
# ============================================================

def is_source_processed(db, source_key: str) -> bool:
    """Has this email/URL been processed before?"""
    result = db.execute(
        sql_text("SELECT 1 FROM processed_sources WHERE source_key = :key LIMIT 1"),
        {"key": source_key},
    ).first()
    return result is not None


def mark_source_processed(db, source_key: str, source_type: str, jobs_count: int, notes: str = ""):
    """Record that we've processed this source so future runs skip it."""
    # Truncate source_key if too long (keys are 500-char VARCHAR)
    key = source_key[:500] if source_key else ""
    db.execute(
        sql_text("""
            INSERT INTO processed_sources (source_key, source_type, jobs_extracted, notes)
            VALUES (:key, :type, :jobs, :notes)
            ON CONFLICT (source_key) DO UPDATE
            SET processed_at = NOW(), jobs_extracted = EXCLUDED.jobs_extracted
        """),
        {"key": key, "type": source_type, "jobs": jobs_count, "notes": notes[:500]},
    )
    db.commit()


# ============================================================
# HELPERS
# ============================================================

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

    for job in extracted_jobs:
        title = job.get("title")
        employer_name = job.get("employer")
        location = job.get("location")
        original_url = job.get("original_url")

        if not title or not original_url:
            continue

        is_likely_ccq = bool(job.get("is_likely_ccq", False))
        ccq_confidence = float(job.get("ccq_confidence", 0) or 0)
        claude_needs_review = bool(job.get("needs_review", False))

        action, is_approved, needs_review = decide_job_status(
            is_likely_ccq, ccq_confidence, claude_needs_review
        )

        if action == "skip":
            logger.info(f"Skipping non-CCQ: {title} (conf: {ccq_confidence})")
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

        js = JobSource(job_id=new_job.id, source_id=src.id, source_url=original_url)
        db.add(js)
        db.commit()

        if action == "save_approved":
            inserted_approved += 1
        else:
            inserted_review += 1

    return {
        "inserted_approved": inserted_approved,
        "inserted_review": inserted_review,
        "skipped_non_ccq": skipped_non_ccq,
        "updated": updated,
    }


# ============================================================
# VOLET 1 — GMAIL
# ============================================================

def process_volet_1_gmail(run_id: int) -> dict:
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

    # Filter out emails already processed in a previous run
    db = get_session()
    try:
        new_emails = [e for e in emails if not is_source_processed(db, e.message_id)]
    finally:
        db.close()

    skipped_cached = len(emails) - len(new_emails)
    logger.info(
        f"Found {len(emails)} emails, {len(new_emails)} new, "
        f"{skipped_cached} already processed (cache hit)"
    )

    if not new_emails:
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0, "cache_hits": skipped_cached}

    all_extracted = []
    ai_calls = 0

    for email in new_emails:
        try:
            jobs = email_parser.extract_jobs_from_email(email)
            ai_calls += 1

            # Mark this email as processed (so we don't re-process it next run)
            db = get_session()
            try:
                mark_source_processed(
                    db, email.message_id, "email",
                    jobs_count=len(jobs),
                    notes=f"from={email.sender[:80]} subject={email.subject[:80]}"
                )
            finally:
                db.close()

            for job in jobs:
                all_extracted.append(job)

        except Exception as e:
            logger.error(f"Failed on email {email.message_id}: {e}", exc_info=True)
            continue

    logger.info(f"Volet 1 extracted {len(all_extracted)} jobs from {len(new_emails)} new emails.")

    if not all_extracted:
        return {
            "jobs_scraped": 0, "jobs_new": 0, "ai_calls": ai_calls,
            "cache_hits": skipped_cached,
        }

    db = get_session()
    try:
        stats = save_extracted_jobs(db, all_extracted, "indeed")
    finally:
        db.close()

    logger.info(
        f"Volet 1 DB writes: {stats['inserted_approved']} approved, "
        f"{stats['inserted_review']} review, {stats['skipped_non_ccq']} skipped, "
        f"{stats['updated']} already in DB (job-level dedup)"
    )

    return {
        "jobs_scraped": len(all_extracted),
        "jobs_new": stats["inserted_approved"] + stats["inserted_review"],
        "jobs_updated": stats["updated"],
        "jobs_flagged": stats["inserted_review"],
        "ai_calls": ai_calls,
        "cache_hits": skipped_cached,
    }


# ============================================================
# VOLET 2 — WEB SEARCH
# ============================================================

def process_volet_2_websearch(run_id: int) -> dict:
    logger.info("-" * 60)
    logger.info("VOLET 2: Web search (Serper)")
    logger.info("-" * 60)

    # Get already-processed URLs so web_search can skip them before fetching
    db = get_session()
    try:
        seen_urls_rows = db.execute(
            sql_text("SELECT source_key FROM processed_sources WHERE source_type = 'webpage'")
        ).all()
        seen_urls = {row[0] for row in seen_urls_rows}
    finally:
        db.close()

    logger.info(f"{len(seen_urls)} URLs already processed in previous runs (cache)")

    try:
        pages = web_search.find_ccq_job_pages(
            max_results_per_query=10,
            skip_urls=seen_urls,
        )
    except TypeError:
        # Backward compat if web_search hasn't been updated to accept skip_urls
        pages = web_search.find_ccq_job_pages(max_results_per_query=10)
        pages = [p for p in pages if p.message_id not in seen_urls]
    except Exception as e:
        logger.error(f"Web search failed: {e}", exc_info=True)
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    if not pages:
        logger.info("No new pages to process.")
        return {"jobs_scraped": 0, "jobs_new": 0, "ai_calls": 0}

    all_extracted = []
    ai_calls = 0

    for page in pages:
        try:
            jobs = email_parser.extract_jobs_from_email(page)
            ai_calls += 1

            # Mark this page URL as processed
            db = get_session()
            try:
                mark_source_processed(
                    db, page.message_id, "webpage",
                    jobs_count=len(jobs),
                    notes=f"subject={page.subject[:80]}"
                )
            finally:
                db.close()

            for job in jobs:
                if not job.get("original_url"):
                    job["original_url"] = page.message_id
                all_extracted.append(job)

        except Exception as e:
            logger.error(f"Failed on page {page.message_id[:80]}: {e}", exc_info=True)
            continue

    logger.info(f"Volet 2 extracted {len(all_extracted)} jobs from {len(pages)} new pages.")

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
        f"{stats['updated']} already in DB (job-level dedup)"
    )

    return {
        "jobs_scraped": len(all_extracted),
        "jobs_new": stats["inserted_approved"] + stats["inserted_review"],
        "jobs_updated": stats["updated"],
        "jobs_flagged": stats["inserted_review"],
        "ai_calls": ai_calls,
    }


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("=" * 60)
    logger.info("Scraper starting up (v4 with source cache)")
    logger.info("=" * 60)

    try:
        db = get_session()
        db.execute(sql_text("SELECT 1"))
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
