"""
APIx — Database Engine / Session Management
===============================================

Single place that knows how to connect to Postgres: a typed Settings object
(reads DATABASE_URL from the environment / a .env file), the SQLAlchemy
engine built from it, and a sessionmaker used both by the CLI loader
(app/db/loader.py, invoked from run_daily_scrape.py) and by FastAPI's
dependency injection (app/api/deps.py).

`pool_pre_ping=True` matters specifically because the loader runs as a
one-shot CLI batch that can take several minutes for a full 30-task scrape
— pre-ping avoids handing back a connection Postgres (or a local Docker
restart) has silently dropped in the meantime.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://apix:apix_dev_password@localhost:5432/apix"


settings = Settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    """FastAPI dependency: yields one Session per request, always closed."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
