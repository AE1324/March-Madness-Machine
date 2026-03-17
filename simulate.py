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


def win_probability(team_a: Team, team_b: Team, round_num: int = 1) -> float:
    import math

    # --- Historical seed vs seed priors (P(better seed wins)) ---

    HIST_R64 = {
        (1, 16): 0.993,
        (2, 15): 0.935,
        (3, 14): 0.855,
        (4, 13): 0.790,
        (5, 12): 0.670,
        (6, 11): 0.620,
        (7, 10): 0.605,
        (8, 9):  0.505,
    }

    HIST_R32 = {
        (1, 8): 0.75, (1, 9): 0.75,
        (2, 7): 0.70, (2, 10): 0.70,
        (3, 6): 0.65, (3, 11): 0.65,
        (4, 5): 0.60, (4, 12): 0.60,
        (5, 13): 0.65, (6, 14): 0.70, (7, 15): 0.75, (8, 16): 0.80,
    }

    HIST_S16 = {
        (1, 4): 0.70, (1, 5): 0.70, (1, 8): 0.80,
        (2, 3): 0.60, (2, 6): 0.65, (2, 7): 0.70,
        (3, 4): 0.55, (3, 5): 0.60, (3, 8): 0.75,
    }

    HIST_E8 = {
        (1, 2): 0.60, (1, 3): 0.65, (1, 4): 0.70,
        (2, 3): 0.55, (2, 4): 0.60,
        (3, 4): 0.55,
    }

    HIST_BY_ROUND = {
        1: HIST_R64,
        2: HIST_R32,
        3: HIST_S16,
        4: HIST_E8,
    }

    # KenPom model weight (logit blend) by round
    ALPHA_BY_ROUND = {
        1: 0.50,
        2: 0.65,
        3: 0.80,
        4: 0.88,
        5: 0.92,
        6: 0.94
    }

    # Round-dependent KenPom logistic scale (later rounds more deterministic)
    SCALE_BY_ROUND = {
        1: 4.8,
        2: 4.7,
        3: 4.6,
        4: 4.5,
        5: 4.4,
        6: 4.3
    }

    # Round-dependent temperature
    TEMP_BY_ROUND = {
        1: 1.10,
        2: 1.04,
        3: 0.96,
        4: 0.92,
        5: 0.90,
        6: 0.88
    }

    # Extreme R64 upset caps (max underdog win probability)
    MAX_UNDERDOG_R64 = {
        (1, 16): 0.015,  # ≈1.5%
        (2, 15): 0.055,
        (3, 14): 0.12,
        (4, 13): 0.20,
    }

    def logistic(diff: float, scale: float) -> float:
        return 1.0 / (1.0 + math.exp(-diff / scale))

    def logit(x: float) -> float:
        return math.log(x / (1 - x))

    def inv_logit(z: float) -> float:
        return 1.0 / (1.0 + math.exp(-z))

    # --- 1) KenPom model probability (AdjEM-based) with round-specific scale ---

    ra = getattr(team_a, "adj_em", None) if hasattr(team_a, "adj_em") else None
    rb = getattr(team_b, "adj_em", None) if hasattr(team_b, "adj_em") else None

    if ra is None or rb is None:
        ra = team_a.rating
        rb = team_b.rating
    if ra is None or rb is None:
        ra = float(17 - team_a.seed)
        rb = float(17 - team_b.seed)

    scale = SCALE_BY_ROUND.get(round_num, 4.5)
    p_model = logistic(ra - rb, scale)

    # --- 2) Historical prior with KenPom-adjusted effective seed gap ---

    sa, sb = team_a.seed, team_b.seed
    better_seed, worse_seed = min(sa, sb), max(sa, sb)

    # base historical table
    hist_table = HIST_BY_ROUND.get(round_num, {})
    p_hist_fav = hist_table.get((better_seed, worse_seed))

    if p_hist_fav is None:
        # fallback seed curve (slightly steeper)
        diff = (worse_seed - better_seed)
        p_hist_fav = logistic(diff, scale=2.0)

    # KenPom rank adjustment (only for early rounds)
    rank_a = getattr(team_a, "kenpom_rank", None)
    rank_b = getattr(team_b, "kenpom_rank", None)
    if rank_a is not None and rank_b is not None and round_num <= 2:
        # positive if B is weaker rank than A (A has lower/better rank)
        rank_gap = (rank_b - rank_a)
        # standardize roughly: assume top ~100 teams matter most
        z_rank = rank_gap / 18.0  # tune denominator if needed
        # effective seed gap: move priors toward KenPom when seeds lie
        seed_gap = worse_seed - better_seed
        effective_seed_gap = seed_gap - 0.35 * z_rank
        p_hist_fav = logistic(effective_seed_gap, scale=2.3)

    if sa == better_seed:
        p_hist = p_hist_fav
    elif sb == better_seed:
        p_hist = 1.0 - p_hist_fav
    else:
        p_hist = 0.5

    # --- 3) Logit-space blend between KenPom model and historical prior ---

    alpha = ALPHA_BY_ROUND.get(round_num, 0.9)

    p_model = min(max(p_model, 1e-6), 1 - 1e-6)
    p_hist = min(max(p_hist, 1e-6), 1 - 1e-6)

    z_model = logit(p_model)
    z_hist = logit(p_hist)

    z_blend = alpha * z_model + (1.0 - alpha) * z_hist
    p = inv_logit(z_blend)

    # --- 4) Round-dependent temperature transform ---

    temp = TEMP_BY_ROUND.get(round_num, 0.9)
    p = min(max(p, 1e-6), 1 - 1e-6)
    z = logit(p)
    p = inv_logit(z / temp)

    # --- 6) Apply R64 extreme upset caps ---

    if round_num == 1 and (better_seed, worse_seed) in MAX_UNDERDOG_R64:
        max_ud = MAX_UNDERDOG_R64[(better_seed, worse_seed)]
        if sa < sb:
            # team_a favorite
            p_ud = 1.0 - p
            if p_ud > max_ud:
                p = 1.0 - max_ud
        else:
            # team_a underdog
            p_ud = p
            if p_ud > max_ud:
                p = max_ud

    

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

        p = win_probability(team1, team2, round_num=game.round)
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

