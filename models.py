from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Float,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from db import Base
from sqlalchemy import JSON 


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    kenpom: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kenpom_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adj_em: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_o: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_tempo: Mapped[float | None] = mapped_column(Float, nullable=True)
    luck: Mapped[float | None] = mapped_column(Float, nullable=True)
    sos_adj_em: Mapped[float | None] = mapped_column(Float, nullable=True)
    sos_adj_o: Mapped[float | None] = mapped_column(Float, nullable=True)
    sos_adj_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    ncsos_adj_em: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Team id={self.id} name={self.name!r} seed={self.seed} region={self.region!r}>"


class TournamentGame(Base):
    __tablename__ = "tournament_games"
    __table_args__ = (UniqueConstraint("slot", name="uq_tournament_games_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    slot: Mapped[str] = mapped_column(String, nullable=False)

    # team1_source and team2_source are strings of the form:
    # - "TEAM-<team_id>" for a fixed team
    # - "WIN-<game_id>" for the winner of a previous game
    team1_source: Mapped[str] = mapped_column(String, nullable=False)
    team2_source: Mapped[str] = mapped_column(String, nullable=False)


class RealResult(Base):
    __tablename__ = "real_results"

    game_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tournament_games.id"), primary_key=True
    )
    winner_team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id"), nullable=False)
    loser_team_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("teams.id"), nullable=True)


class Bracket(Base):
    __tablename__ = "brackets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)

    # Packed bracket outcomes (63 games for a 64-team tournament).
    # Bit i corresponds to the i'th game in TournamentGame ordered by (round, id).
    # Bit value 1 => team1_source won that game; 0 => team2_source won.
    result_bits: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    champion_team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("teams.id"), nullable=True, index=True
    )

    picks: Mapped[list["BracketPick"]] = relationship("BracketPick", back_populates="bracket")


class BracketPick(Base):
    __tablename__ = "bracket_picks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bracket_id: Mapped[int] = mapped_column(Integer, ForeignKey("brackets.id"), index=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("tournament_games.id"), index=True)
    predicted_winner_team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id"), nullable=False)

    bracket: Mapped[Bracket] = relationship("Bracket", back_populates="picks")

