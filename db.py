from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# Default to a local SQLite database. You can change this to a PostgreSQL URL
# like "postgresql+psycopg2://user:password@localhost:5432/brackets"
import os
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///brackets.db")



engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables in the database."""
    from models import Team, TournamentGame, RealResult, Bracket, BracketPick  # noqa: F401

    Base.metadata.create_all(bind=engine)

