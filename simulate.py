from __future__ import annotations

import math
import random
from typing import Dict

from sqlalchemy.orm import Session

from models import Team, TournamentGame, Bracket, BracketPick


def win_probability(team_a: Team, team_b: Team) -> float:
    """
    Compute probability that team_a beats team_b.

    Simple rating-based logistic model:
        P(A wins) = 1 / (1 + exp(-(rating_a - rating_b) / scale))

    If ratings are missing, fall back to seed-based heuristic
    (lower seed number is better).
    """
    rating_a = team_a.rating
    rating_b = team_b.rating

    if rating_a is None or rating_b is None:
        # Seed-based heuristic: convert seeds to pseudo-ratings
        # Better seeds (1) get higher pseudo-rating than worse seeds (16).
        pseudo_a = 17 - team_a.seed
        pseudo_b = 17 - team_b.seed
        diff = pseudo_a - pseudo_b
        scale = 3.0
        return 1.0 / (1.0 + math.exp(-diff / scale))

    diff = rating_a - rating_b
    scale = 5.0
    return 1.0 / (1.0 + math.exp(-diff / scale))


def _resolve_team(source: str, teams_by_id: Dict[int, Team], winners_by_key: Dict[str, int]) -> Team:
    """
    Resolve a team for a game based on its source.

    - "TEAM-<team_id>": fixed team.
    - "WIN-<game_id>": winner of an earlier game.
    """
    if source.startswith("TEAM-"):
        team_id = int(source.split("-", 1)[1])
        return teams_by_id[team_id]
    if source.startswith("WIN-"):
        game_id = int(source.split("-", 1)[1])
        key = f"WIN-{game_id}"
        team_id = winners_by_key[key]
        return teams_by_id[team_id]
    raise ValueError(f"Unknown team source format: {source}")


def simulate_single_bracket(session: Session, model_version: str = "v1") -> int:
    """
    Simulate a single full tournament bracket.

    Returns the created bracket ID.
    """
    teams = {t.id: t for t in session.query(Team).all()}
    games = (
        session.query(TournamentGame)
        .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
        .all()
    )

    if not games:
        raise RuntimeError("No tournament games found. Load a bracket first.")

    bracket = Bracket(model_version=model_version)
    session.add(bracket)
    session.flush()  # assign bracket.id

    winners_by_key: Dict[str, int] = {}
    picks: list[BracketPick] = []

    for game in games:
        team1 = _resolve_team(game.team1_source, teams, winners_by_key)
        team2 = _resolve_team(game.team2_source, teams, winners_by_key)

        p = win_probability(team1, team2)
        if random.random() < p:
            winner = team1
        else:
            winner = team2

        winners_by_key[f"WIN-{game.id}"] = winner.id

        picks.append(
            BracketPick(
                bracket_id=bracket.id,
                game_id=game.id,
                predicted_winner_team_id=winner.id,
            )
        )

    session.bulk_save_objects(picks)
    session.commit()
    return bracket.id


def generate_brackets(session: Session, n: int, batch_size: int = 10_000, model_version: str = "v1") -> None:
    """
    Generate many brackets, committing in batches for performance.
    """
    remaining = n
    while remaining > 0:
        current_batch = min(batch_size, remaining)
        for _ in range(current_batch):
            simulate_single_bracket(session, model_version=model_version)
        remaining -= current_batch

