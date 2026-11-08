"""Database configuration for DonorFlow backend."""

from collections.abc import Generator
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/donorflow",
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and always close it after use."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
