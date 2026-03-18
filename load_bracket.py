from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from models import Team, TournamentGame


def load_bracket_from_json(session: Session, json_path: str) -> None:
    """
    Load teams and tournament games from a JSON file.

    Expected JSON structure (see bracket_example.json for a concrete example):

    {
      "teams": [
        {
          "id": 1,
          "name": "Gonzaga",
          "seed": 1,
          "region": "West",
          "rating": 30.5
        },
        ...
      ],
      "games": [
        {
          "round": 1,
          "region": "West",
          "slot": "W-1",
          "team1_source": "TEAM-1",
          "team2_source": "TEAM-16"
        },
        {
          "round": 2,
          "region": "West",
          "slot": "W-9",
          "team1_source": "WIN-1",
          "team2_source": "WIN-2"
        },
        ...
      ]
    }
    """
    path = Path(json_path)
    if not path.is_file():
        raise FileNotFoundError(f"Bracket JSON file not found: {json_path}")

    with path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)

    teams_data = data.get("teams", [])
    games_data = data.get("games", [])

    if not teams_data or not games_data:
        raise ValueError("Bracket JSON must contain non-empty 'teams' and 'games' lists.")

    # Clear existing teams and games to avoid conflicts
    session.query(TournamentGame).delete()
    session.query(Team).delete()
    session.flush()

    teams: list[Team] = []
    for t in teams_data:
        teams.append(
            Team(
                id=t["id"],
                name=t["name"],
                seed=t["seed"],
                region=t["region"],
                rating=t.get("rating"),
            )
        )

    # IMPORTANT:
    # `MM_2026.json` encodes upstream winners as "WIN-<game_id>" where the <game_id>
    # is the numeric ID referenced by the JSON game graph.
    #
    # To make "WIN-1", "WIN-2", ... resolve correctly, we must ensure that the
    # database primary keys `tournament_games.id` match that JSON numbering.
    # We do that by assigning explicit IDs based on the games array position.
    games: list[TournamentGame] = []
    for idx, g in enumerate(games_data, start=1):
        games.append(
            TournamentGame(
                id=idx,
                round=g["round"],
                region=g.get("region"),
                slot=g["slot"],
                team1_source=g["team1_source"],
                team2_source=g["team2_source"],
            )
        )

    session.bulk_save_objects(teams)
    session.bulk_save_objects(games)
    session.commit()

