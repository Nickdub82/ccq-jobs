"""FastAPI entry point. Run: uvicorn main:app --reload"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routes import jobs, admin

app = FastAPI(
    title="CCQ Jobs Portal API",
    description="Public job aggregation for Quebec construction workers. "
                "NOT affiliated with the CCQ. All listings link back to the original source.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(jobs.router)
app.include_router(admin.router)


@app.get("/")
def root():
    return {
        "service": "ccq-jobs-api",
        "status": "ok",
        "disclaimer": "Aggregates publicly available job postings. Not affiliated with CCQ."
    }


@app.get("/health")
def health():
    return {"status": "healthy"}
