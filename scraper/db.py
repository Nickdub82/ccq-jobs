"""Database connection for scraper.

Design notes:
    When Railway runs `cd scraper && python run.py`, Python's CWD is /app/scraper,
    so /app/scraper is first on sys.path. That means when backend/models.py does
    `from db import Base`, it finds THIS file (scraper/db.py) instead of backend/db.py.

    To keep things working, we do the setup BEFORE any other module imports so that
    both contexts end up using the same SQLAlchemy Base, engine, and SessionLocal.
"""
import sys
from pathlib import Path

# Make backend/ importable first (we need its modules)
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from config import settings

# Normalize DB URL for psycopg v3
db_url = settings.database_url
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# Base is re-exported here so `from db import Base` works whether the importer
# is backend/ or scraper/ (both contexts see the SAME Base instance).
Base = declarative_base()


def get_session():
    return SessionLocal()


def get_db():
    """FastAPI-style dependency (kept for compatibility with backend imports)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
