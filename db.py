from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# Default to a local SQLite database. You can change this to a PostgreSQL URL
# like "postgresql+psycopg2://user:password@localhost:5432/brackets"
import os
DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/brackets"
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Set it to your Postgres URL before running.")



engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables in the database."""
    from models import Team, TournamentGame, RealResult, Bracket, BracketPick  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # Basic "no-migrations" schema upgrade for local development.
    # Base.metadata.create_all() won't add new columns to existing tables,
    # so we add the new bracket outcome columns if they are missing.
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        # Brackets: packed outcomes storage.
        if "brackets" in insp.get_table_names():
            existing_cols = {c["name"] for c in insp.get_columns("brackets")}
            if "result_bits" not in existing_cols:
                conn.execute(text("ALTER TABLE brackets ADD COLUMN result_bits BIGINT"))
            if "survival_index" not in existing_cols:
                conn.execute(
                    text("ALTER TABLE brackets ADD COLUMN survival_index INTEGER")
                )
                # Initialize legacy rows (if any) to "alive through all games".
                conn.execute(
                    text("UPDATE brackets SET survival_index = 63 WHERE survival_index IS NULL")
                )
            if "champion_team_id" not in existing_cols:
                conn.execute(
                    text("ALTER TABLE brackets ADD COLUMN champion_team_id INTEGER")
                )

        # Postgres-only performance indexes for survival updates.
        if getattr(engine.dialect, "name", "") == "postgresql":
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_survival_active ON brackets (survival_index) WHERE survival_index >= 0"
                )
            )

        # Teams: KenPom JSON and derived numeric columns.
        if "teams" in insp.get_table_names():
            existing_cols = {c["name"] for c in insp.get_columns("teams")}
            team_cols: dict[str, str] = {
                "kenpom": "JSON",
                "kenpom_rank": "INTEGER",
                "adj_em": "FLOAT",
                "adj_o": "FLOAT",
                "adj_d": "FLOAT",
                "adj_tempo": "FLOAT",
                "luck": "FLOAT",
                "sos_adj_em": "FLOAT",
                "sos_adj_o": "FLOAT",
                "sos_adj_d": "FLOAT",
                "ncsos_adj_em": "FLOAT",
                "rating": "FLOAT",
            }
            for col_name, col_type in team_cols.items():
                if col_name not in existing_cols:
                    conn.execute(
                        text(f"ALTER TABLE teams ADD COLUMN {col_name} {col_type}")
                    )

