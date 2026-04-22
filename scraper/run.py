"""
Main scraper runner. This is the entry point invoked by the Railway cron every 2h.

Pipeline:
    1. Start a scraping_run log entry
    2. Scrape Indeed (or all active sources)
    3. Compute fingerprint for each raw job → dedup against existing DB
    4. For NEW jobs only, send to Claude for classification
    5. Insert/update jobs in DB based on Claude's output
    6. Remove jobs from DB that weren't seen this run (source removed them)
    7. Finalize the scraping_run log with stats

Run manually: python scraper/run.py
"""
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

# Ensure backend models are importable
from db import get_session
from models import Job, Employer, Source, JobSource, ScrapingRun

from indeed import scrape_indeed, RawJobListing
from dedup import make_fingerprint, normalize_employer_name
from ai_filter import classify_batch, estimate_cost

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scraper.run")


def get_or_create_source(db, name: str) -> Source:
    src = db.query(Source).filter(Source.name == name).first()
    if not src:
        raise RuntimeError(f"Source '{name}' not found in DB. Run db/schema.sql first.")
    return src


def get_or_create_employer(db, name: str) -> Optional[Employer]:
    if not name:
        return None
    normalized = normalize_employer_name(name)
    if not normalized:
        return None
    emp = db.query(Employer).filter(Employer.normalized_name == normalized).first()
    if not emp:
        emp = Employer(name=name, normalized_name=normalized)
        db.add(emp)
        db.flush()
    return emp


def process_source(source_name: str, run_id: int) -> dict:
    """
    Scrape one source, run AI classification, upsert into DB, prune removed.

    Returns a stats dict.
    """
    stats = {
        "jobs_scraped": 0,
        "jobs_new": 0,
        "jobs_updated": 0,
        "jobs_removed": 0,
        "jobs_flagged": 0,
        "ai_calls": 0,
        "ai_cost": 0.0,
    }

    db = get_session()
    try:
        source = get_or_create_source(db, source_name)

        # --- 1. SCRAPE ---
        logger.info(f"Scraping {source_name}...")
        if source_name == "indeed":
            raw_jobs = scrape_indeed()
        else:
            raw_jobs = []
            logger.warning(f"No scraper module for {source_name}, skipping.")

        stats["jobs_scraped"] = len(raw_jobs)
        if not raw_jobs:
            logger.info(f"No jobs scraped from {source_name}.")
            return stats

        # --- 2. DEDUP against existing DB ---
        # For each raw job, compute a fingerprint. Separate into "new" vs "seen".
        raw_with_fp = []
        for rj in raw_jobs:
            fp = make_fingerprint(rj.employer_name or "", rj.title, rj.location_text or "")
            raw_with_fp.append((fp, rj))

        fingerprints_this_run = {fp for fp, _ in raw_with_fp}
        existing_jobs = (
            db.query(Job)
            .filter(Job.fingerprint.in_(list(fingerprints_this_run)))
            .all()
        )
        existing_by_fp = {j.fingerprint: j for j in existing_jobs}

        new_items = [(fp, rj) for fp, rj in raw_with_fp if fp not in existing_by_fp]
        seen_items = [(fp, rj) for fp, rj in raw_with_fp if fp in existing_by_fp]

        logger.info(
            f"{len(new_items)} new jobs, {len(seen_items)} already known."
        )

        # --- 3. UPDATE last_seen_at for jobs we saw again ---
        now = datetime.now(timezone.utc)
        for fp, rj in seen_items:
            job = existing_by_fp[fp]
            job.last_seen_at = now
            # Also refresh the job_sources entry for this source
            js = next((s for s in job.job_sources if s.source_id == source.id), None)
            if js:
                js.last_seen_at = now
            else:
                db.add(JobSource(
                    job_id=job.id, source_id=source.id,
                    source_url=rj.original_url,
                ))
        stats["jobs_updated"] = len(seen_items)
        db.commit()

        # --- 4. CLAUDE CLASSIFICATION on new items only ---
        if new_items:
            raw_dicts = [rj.to_dict() for _, rj in new_items]

            # Batch to avoid giant prompts — max 10 per call
            BATCH_SIZE = 10
            classified_all = []
            for i in range(0, len(raw_dicts), BATCH_SIZE):
                batch = raw_dicts[i : i + BATCH_SIZE]
                try:
                    classified = classify_batch(batch)
                    # Correct the index offsets for our flat list
                    for c in classified:
                        c["index"] = c["index"] + i
                    classified_all.extend(classified)
                    stats["ai_calls"] += 1
                except Exception as e:
                    logger.exception(f"Claude call failed for batch {i}: {e}")
                    continue

            # --- 5. INSERT new jobs into DB ---
            for c in classified_all:
                idx = c.get("index", -1)
                if idx < 0 or idx >= len(new_items):
                    continue

                fp, rj = new_items[idx]

                # Skip irrelevant jobs entirely — don't pollute DB
                if not c.get("is_relevant", False):
                    continue

                employer = get_or_create_employer(db, c.get("employer_name") or rj.employer_name)

                needs_review = c.get("needs_review", False)
                confidence = c.get("confidence", 0.0)
                # Auto-approve only if high confidence and not flagged for review
                is_approved = (not needs_review) and confidence >= 0.85

                job = Job(
                    fingerprint=fp,
                    external_id=rj.external_id,
                    title=c.get("title") or rj.title,
                    description=c.get("description_clean") or rj.description_snippet,
                    employer_id=employer.id if employer else None,
                    location_text=rj.location_text,
                    city=c.get("city"),
                    region=c.get("region"),
                    address=c.get("address"),
                    job_type=c.get("job_type"),
                    trade=c.get("trade"),
                    salary_text=c.get("salary_text") or rj.salary_text,
                    is_ccq=c.get("is_ccq", False),
                    original_url=rj.original_url,
                    source_id=source.id,
                    posted_at=None,  # Indeed "posted_text" is relative; parse in V2
                    first_seen_at=now,
                    last_seen_at=now,
                    ai_confidence=confidence,
                    ai_notes=c.get("notes"),
                    is_approved=is_approved,
                    needs_review=needs_review,
                )
                db.add(job)
                db.flush()

                db.add(JobSource(
                    job_id=job.id,
                    source_id=source.id,
                    source_url=rj.original_url,
                ))

                stats["jobs_new"] += 1
                if needs_review:
                    stats["jobs_flagged"] += 1

            db.commit()

        # --- 6. REMOVE jobs from this source that we didn't see this run ---
        # A job is "gone" from this source if its job_sources entry for this source
        # wasn't refreshed this run. If a job has no remaining sources, delete it.
        stale = (
            db.query(JobSource)
            .filter(
                JobSource.source_id == source.id,
                JobSource.last_seen_at < now.replace(minute=0, second=0, microsecond=0),
            )
            .all()
        )
        removed_count = 0
        for js in stale:
            # Only remove if this is the ONLY source for this job
            job = db.query(Job).filter(Job.id == js.job_id).first()
            if job and len(job.job_sources) <= 1:
                db.delete(job)
                removed_count += 1
            else:
                db.delete(js)
        stats["jobs_removed"] = removed_count
        db.commit()

        logger.info(f"Done with {source_name}. Stats: {stats}")
        return stats

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    """Entry point — run all active sources."""
    db = get_session()

    # Create a run log entry
    run = ScrapingRun(status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    aggregate = {
        "jobs_scraped": 0, "jobs_new": 0, "jobs_updated": 0,
        "jobs_removed": 0, "jobs_flagged": 0, "ai_calls": 0,
    }
    error_msg = None

    try:
        # Get all active sources
        active_sources = db.query(Source).filter(Source.is_active == True).all()
        for src in active_sources:
            try:
                stats = process_source(src.name, run_id)
                for k in aggregate:
                    aggregate[k] += stats.get(k, 0)
            except Exception as e:
                logger.exception(f"Source {src.name} failed: {e}")
                error_msg = (error_msg or "") + f"\n{src.name}: {e}"
    except Exception as e:
        logger.exception(f"Run failed: {e}")
        error_msg = str(e)
    finally:
        # Refresh run entity and finalize
        run = db.query(ScrapingRun).filter(ScrapingRun.id == run_id).first()
        run.finished_at = datetime.now(timezone.utc)
        run.status = "failed" if error_msg else "success"
        run.jobs_scraped = aggregate["jobs_scraped"]
        run.jobs_new = aggregate["jobs_new"]
        run.jobs_updated = aggregate["jobs_updated"]
        run.jobs_removed = aggregate["jobs_removed"]
        run.jobs_flagged = aggregate["jobs_flagged"]
        run.ai_calls = aggregate["ai_calls"]
        run.error_message = error_msg
        db.commit()
        db.close()

    logger.info(f"Run {run_id} complete. Final stats: {aggregate}")
    return aggregate


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Fatal error in scraper run.")
        sys.exit(1)
