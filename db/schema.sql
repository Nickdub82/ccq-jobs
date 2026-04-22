-- ============================================================
-- CCQ Jobs Portal — Database schema
-- Run once: psql $DATABASE_URL -f db/schema.sql
-- ============================================================

-- Sources (Indeed, Jobboom, etc.)
CREATE TABLE IF NOT EXISTS sources (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL UNIQUE,       -- 'indeed', 'jobboom', etc.
    display_name    VARCHAR(200) NOT NULL,              -- 'Indeed.ca'
    base_url        VARCHAR(500) NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Employers (deduplicated by normalized name)
CREATE TABLE IF NOT EXISTS employers (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(300) NOT NULL,
    normalized_name VARCHAR(300) NOT NULL UNIQUE,       -- lowercased, stripped for dedup
    website         VARCHAR(500),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_employers_normalized ON employers(normalized_name);

-- Jobs (main table)
CREATE TABLE IF NOT EXISTS jobs (
    id                  SERIAL PRIMARY KEY,

    -- Identity & dedup
    fingerprint         VARCHAR(64) NOT NULL UNIQUE,   -- hash of employer+title+location
    external_id         VARCHAR(200),                  -- source's own ID when available

    -- Core fields
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    employer_id         INTEGER REFERENCES employers(id) ON DELETE SET NULL,

    -- Location
    location_text       VARCHAR(300),                  -- raw text from listing
    city                VARCHAR(100),
    region              VARCHAR(100),                  -- 'Montreal', 'Laval', etc.
    address             VARCHAR(500),                  -- full address if available
    latitude            DOUBLE PRECISION,
    longitude           DOUBLE PRECISION,

    -- Job details
    job_type            VARCHAR(50),                   -- 'full-time', 'part-time', 'contract'
    trade              VARCHAR(100),                   -- 'peintre', 'painter'
    salary_text         VARCHAR(200),                  -- raw text, no interpretation
    is_ccq              BOOLEAN DEFAULT FALSE,         -- confirmed CCQ job

    -- Source tracking
    original_url        VARCHAR(1000) NOT NULL,        -- ALWAYS link back to source
    source_id           INTEGER REFERENCES sources(id),
    posted_at           TIMESTAMPTZ,                   -- date posted on source
    first_seen_at       TIMESTAMPTZ DEFAULT NOW(),     -- first time scraper saw it
    last_seen_at        TIMESTAMPTZ DEFAULT NOW(),     -- last time scraper confirmed it

    -- AI flags
    ai_confidence       REAL,                          -- 0.0 to 1.0 from Claude
    ai_notes            TEXT,                          -- Claude's reasoning
    is_approved         BOOLEAN DEFAULT FALSE,         -- passed review (auto-true if confidence >= 0.85)
    needs_review        BOOLEAN DEFAULT FALSE,         -- Claude flagged uncertain

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint    ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_region         ON jobs(region);
CREATE INDEX IF NOT EXISTS idx_jobs_trade          ON jobs(trade);
CREATE INDEX IF NOT EXISTS idx_jobs_is_ccq         ON jobs(is_ccq);
CREATE INDEX IF NOT EXISTS idx_jobs_is_approved    ON jobs(is_approved);
CREATE INDEX IF NOT EXISTS idx_jobs_needs_review   ON jobs(needs_review);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen      ON jobs(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at      ON jobs(posted_at DESC);

-- Job sources (many-to-many: same job can appear on multiple sites)
CREATE TABLE IF NOT EXISTS job_sources (
    id              SERIAL PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    source_url      VARCHAR(1000) NOT NULL,
    first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(job_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_job_sources_job ON job_sources(job_id);

-- Scraping runs log
CREATE TABLE IF NOT EXISTS scraping_runs (
    id                  SERIAL PRIMARY KEY,
    source_id           INTEGER REFERENCES sources(id),
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              VARCHAR(20) DEFAULT 'running',  -- running, success, failed
    jobs_scraped        INTEGER DEFAULT 0,
    jobs_new            INTEGER DEFAULT 0,
    jobs_updated        INTEGER DEFAULT 0,
    jobs_removed        INTEGER DEFAULT 0,
    jobs_flagged        INTEGER DEFAULT 0,
    ai_calls            INTEGER DEFAULT 0,
    ai_cost_estimate    DECIMAL(10,4),
    error_message       TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_scraping_runs_started ON scraping_runs(started_at DESC);

-- Seed the sources table
INSERT INTO sources (name, display_name, base_url, is_active)
VALUES
    ('indeed',    'Indeed.ca',   'https://ca.indeed.com',   TRUE),
    ('jobboom',   'Jobboom',     'https://www.jobboom.com', FALSE),
    ('jobillico', 'Jobillico',   'https://www.jobillico.com', FALSE),
    ('facebook',  'Facebook',    'https://www.facebook.com', FALSE)
ON CONFLICT (name) DO NOTHING;

-- Auto-update `updated_at` on jobs table
CREATE OR REPLACE FUNCTION update_jobs_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jobs_updated_at ON jobs;
CREATE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_jobs_updated_at();
