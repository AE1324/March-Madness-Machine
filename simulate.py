from __future__ import annotations

import math
import random
from typing import Dict

from sqlalchemy.orm import Session

from models import Team, TournamentGame, Bracket, BracketPick

_NUMERIC_METRICS = [
    "adj_em",
    "adj_o",
    "adj_d",
    "adj_tempo",
    "luck",
    "sos_adj_em",
    "sos_adj_o",
    "sos_adj_d",
    "ncsos_adj_em",
]

_METRIC_WEIGHTS = {
    "adj_em": 1.30,
    "adj_o": 0.35,
    "adj_d": 0.35,      # lower is better (we flip sign below)
    "adj_tempo": 0.08,
    "luck": 0.05,
    "sos_adj_em": 0.15,
    "sos_adj_o": 0.08,
    "sos_adj_d": 0.08,  # lower is better (we flip sign below)
    "ncsos_adj_em": 0.08,
}

_UPSET_TABLE_BETTER_SEED_WINS = {
    (1, 16): 0.99,
    (2, 15): 0.93,
    (3, 14): 0.85,
    (4, 13): 0.79,
    (5, 12): 0.65,
    (6, 11): 0.63,
    (7, 10): 0.60,
    (8, 9): 0.50,
}

def _zscore_params(teams: list[Team]) -> dict[str, tuple[float, float]]:
    vals = {m: [] for m in _NUMERIC_METRICS}
    for t in teams:
        kp = getattr(t, "kenpom", None) or {}
        for m in _NUMERIC_METRICS:
            v = kp.get(m)
            if isinstance(v, (int, float)):
                vals[m].append(float(v))

    params = {}
    for m, arr in vals.items():
        if not arr:
            params[m] = (0.0, 1.0)
            continue
        mean = sum(arr) / len(arr)
        var = sum((x - mean) ** 2 for x in arr) / max(1, (len(arr) - 1))
        std = math.sqrt(var)
        if std < 1e-6:
            std = 1.0
        params[m] = (mean, std)
    return params

def _z(kp: dict, metric: str, params: dict[str, tuple[float, float]]) -> float:
    mean, std = params[metric]
    v = kp.get(metric)
    if not isinstance(v, (int, float)):
        return 0.0
    return (float(v) - mean) / std


def win_probability(team_a: Team, team_b: Team, round_num: int = 1, temperature: float = 0.80) -> float:
    import math

    def logistic(diff: float, scale: float) -> float:
        return 1.0 / (1.0 + math.exp(-(diff / scale)))

    # --- 1) Base model probability from KenPom AdjEM (fallback to rating, then seed) ---
    ra = getattr(team_a, "adj_em", None) if hasattr(team_a, "adj_em") else None
    rb = getattr(team_b, "adj_em", None) if hasattr(team_b, "adj_em") else None
    if ra is None or rb is None:
        ra = team_a.rating
        rb = team_b.rating
    if ra is None or rb is None:
        ra = float(17 - team_a.seed)
        rb = float(17 - team_b.seed)

    # Smaller scale => more chalky favorites
    p_model = logistic(ra - rb, scale=5.0)

    # --- 2) Round-of-64 seed-based caps (P(better seed wins)) ---
    # Set these to what you consider "normal". If you want this year more chalky,
    # increase the favorites a bit (e.g. 0.995 for 1v16).
    r64_fav_win = {
        (1, 16): 0.995,  # 0.5% upset
        (2, 15): 0.97,
        (3, 14): 0.90,
        (4, 13): 0.85,
        (5, 12): 0.75,
        (6, 11): 0.64,
        (7, 10): 0.60,
        (8, 9): 0.50,
    }

    sa, sb = team_a.seed, team_b.seed
    better, worse = min(sa, sb), max(sa, sb)

    # --- 3) Apply caps in Round 1 ---
    # Convert cap to "P(team_a wins)" and clamp the model to never exceed upset limits.
    if round_num == 1 and (better, worse) in r64_fav_win:
        p_fav = r64_fav_win[(better, worse)]
        p_underdog = 1.0 - p_fav

        if sa < sb:
            # team_a is favorite
            p_cap = p_fav
            # force at least p_cap (don't allow model to make favorite too weak)
            p = max(p_model, p_cap)
        elif sb < sa:
            # team_a is underdog
            p_cap = p_underdog
            # force at most p_cap (don't allow model to give underdog too much)
            p = min(p_model, p_cap)
        else:
            p = p_model
    else:
        p = p_model

    # --- 4) Temperature: <1 more chalk, >1 more random ---
    p = min(max(p, 1e-6), 1 - 1e-6)
    logit = math.log(p / (1 - p))
    p = 1.0 / (1.0 + math.exp(-(logit / temperature)))
    return float(min(max(p, 0.0), 1.0))


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
    zparams = _zscore_params(list(teams.values()))
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

        p = win_probability(team1, team2, round_num=game.round, temperature=0.80)
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

