"""FastAPI application entry point for DonorFlow."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from backend.database import Base, engine
from backend.routers import ai, auth, donations, donors, documents, ocr

app = FastAPI(title="ДонорРХТУ API", version="1.0.0", description="API учета доноров и донорских донаций РХТУ")


@app.on_event("startup")
def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            for statement in (
                "ALTER TABLE donations ADD COLUMN IF NOT EXISTS medical_center VARCHAR(255)",
                "ALTER TABLE donations ADD COLUMN IF NOT EXISTS certificate_number VARCHAR(128)",
                "ALTER TABLE donations ADD COLUMN IF NOT EXISTS file_path VARCHAR(512)",
            ):
                connection.execute(text(statement))

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

for api_router in (auth.router, donors.router, donations.router, documents.router, ai.router, ocr.router):
    app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
