from __future__ import annotations

import math
import random
from datetime import datetime
import io
import re
from typing import Callable, Dict, NamedTuple

from sqlalchemy.orm import Session

from models import Team, TournamentGame, Bracket

# --- Constants for the win-probability model (kept module-level for speed) ---

HIST_R64: dict[tuple[int, int], float] = {
    (1, 16): 0.993,
    (2, 15): 0.935,
    (3, 14): 0.855,
    (4, 13): 0.790,
    (5, 12): 0.650,
    (6, 11): 0.620,
    (7, 10): 0.605,
    (8, 9): 0.505,
}

HIST_R32: dict[tuple[int, int], float] = {
    (1, 8): 0.75,
    (1, 9): 0.75,
    (2, 7): 0.70,
    (2, 10): 0.70,
    (3, 6): 0.65,
    (3, 11): 0.65,
    (4, 5): 0.60,
    (4, 12): 0.60,
    (5, 13): 0.65,
    (6, 14): 0.70,
    (7, 15): 0.75,
    (8, 16): 0.80,
}

HIST_S16: dict[tuple[int, int], float] = {
    (1, 4): 0.70,
    (1, 5): 0.70,
    (1, 8): 0.80,
    (2, 3): 0.60,
    (2, 6): 0.65,
    (2, 7): 0.70,
    (3, 4): 0.55,
    (3, 5): 0.60,
    (3, 8): 0.75,
}

HIST_E8: dict[tuple[int, int], float] = {
    (1, 2): 0.60,
    (1, 3): 0.65,
    (1, 4): 0.70,
    (2, 3): 0.55,
    (2, 4): 0.60,
    (3, 4): 0.55,
}

HIST_BY_ROUND: dict[int, dict[tuple[int, int], float]] = {
    1: HIST_R64,
    2: HIST_R32,
    3: HIST_S16,
    4: HIST_E8,
}

ALPHA_BY_ROUND: dict[int, float] = {1: 0.48, 2: 0.60, 3: 0.74, 4: 0.82, 5: 0.88, 6: 0.90}

SCALE_BY_ROUND: dict[int, float] = {
    1: 5.4,
    2: 5.0,
    3: 4.7,
    4: 4.5,
    5: 4.4,
    6: 4.3,
}

TEMP_BY_ROUND: dict[int, float] = {1: 1.10, 2: 1.04, 3: 0.96, 4: 0.92, 5: 0.90, 6: 0.88}

MAX_UNDERDOG_R64: dict[tuple[int, int], float] = {
    (1, 16): 0.015,
    (2, 15): 0.055,
    (3, 14): 0.12,
    (4, 13): 0.20,
}


def _logistic(diff: float, scale: float) -> float:
    return 1.0 / (1.0 + math.exp(-diff / scale))


def _logit(x: float) -> float:
    return math.log(x / (1.0 - x))


def _inv_logit(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def shock_sd_by_round(round_num: int) -> float:
    """
    Round-dependent performance variance (KenPom points shock SD).

    Calibrated to reduce unrealistic late-round chaos.
    """
    return {
        1: 1.9,  # Round of 64
        2: 1.6,  # Round of 32
        3: 1.25,  # Sweet 16
        4: 1.05,  # Elite 8
        5: 0.95,  # Final Four
        6: 0.90,  # Championship
    }.get(round_num, 1.0)


def win_probability_fast(
    *,
    strength_base_a: float,
    strength_base_b: float,
    seed_a: int,
    seed_b: int,
    kenpom_rank_a: float,
    kenpom_rank_b: float,
    round_num: int,
    region_noise: float,
    strength_shock_a: float,
    strength_shock_b: float,
) -> float:
    """
    Fast numeric win-probability: avoids Team attribute lookups.
    kenpom_rank_* must be >=0 when present; pass -1 when missing.
    """
    ra_eff = strength_base_a + strength_shock_a
    rb_eff = strength_base_b + strength_shock_b

    raw_gap = ra_eff - rb_eff
    gap = math.copysign(abs(raw_gap) ** 0.8, raw_gap)

    scale = SCALE_BY_ROUND.get(round_num, 4.5)
    p_model = _logistic(gap, scale)

    sa, sb = seed_a, seed_b
    better_seed, worse_seed = (sa, sb) if sa <= sb else (sb, sa)

    hist_table = HIST_BY_ROUND.get(round_num, {})
    p_hist_fav = hist_table.get((better_seed, worse_seed))
    if p_hist_fav is None:
        diff = worse_seed - better_seed
        p_hist_fav = _logistic(diff, scale=2.0)

    # KenPom rank adjustment (only for early rounds)
    if round_num <= 2 and kenpom_rank_a >= 0 and kenpom_rank_b >= 0:
        rank_gap = (kenpom_rank_b - kenpom_rank_a)
        z_rank = rank_gap / 18.0
        seed_gap = worse_seed - better_seed
        effective_seed_gap = seed_gap - 0.35 * z_rank
        p_hist_fav = _logistic(effective_seed_gap, scale=2.3)

    if sa == better_seed:
        p_hist = p_hist_fav
    elif sb == better_seed:
        p_hist = 1.0 - p_hist_fav
    else:
        p_hist = 0.5

    alpha = ALPHA_BY_ROUND.get(round_num, 0.9)

    p_model = min(max(p_model, 1e-6), 1 - 1e-6)
    p_hist = min(max(p_hist, 1e-6), 1 - 1e-6)

    z_model = _logit(p_model)
    z_hist = _logit(p_hist)

    z_blend = alpha * z_model + (1.0 - alpha) * z_hist
    z_blend += region_noise
    p = _inv_logit(z_blend)

    temp = TEMP_BY_ROUND.get(round_num, 0.9)
    p = min(max(p, 1e-6), 1 - 1e-6)
    z = _logit(p)
    p = _inv_logit(z / temp)

    # Extreme R64 upset caps
    if round_num == 1 and (better_seed, worse_seed) in MAX_UNDERDOG_R64:
        max_ud = MAX_UNDERDOG_R64[(better_seed, worse_seed)]
        if sa < sb:
            p_ud = 1.0 - p  # team_a is favorite, p is P(team_a wins)
            if p_ud > max_ud:
                p = 1.0 - max_ud
        else:
            p_ud = p  # team_a is underdog
            if p_ud > max_ud:
                p = max_ud

    return float(min(max(p, 0.0), 1.0))


class _TeamFast(NamedTuple):
    strength_base: list[float]
    seed: list[int]
    kenpom_rank: list[float]  # -1 when missing


class _GameSpec(NamedTuple):
    round_num: int
    key_idx: int
    team1_kind: int  # 0 fixed team, 1 upstream winner
    team1_val: int   # team_id when fixed, upstream game_index when upstream
    team2_kind: int
    team2_val: int



def win_probability(
    team_a: Team,
    team_b: Team,
    round_num: int = 1,
    region_noise: float = 0.0,
    strength_shock_a: float = 0.0,
    strength_shock_b: float = 0.0,
) -> float:
    # --- Historical seed vs seed priors (P(better seed wins)) ---

    HIST_R64 = {
        (1, 16): 0.993,
        (2, 15): 0.935,
        (3, 14): 0.855,
        (4, 13): 0.790,
        (5, 12): 0.650,
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
        1: 0.48,
        2: 0.60,
        3: 0.74,
        4: 0.82,
        5: 0.88,
        6: 0.90,
    }

    # Round-dependent KenPom logistic scale (later rounds more deterministic)
    SCALE_BY_ROUND = {
        1: 5.4,
        2: 5.0,
        3: 4.7,
        4: 4.5,
        5: 4.4,
        6: 4.3,
    }

    # Round-dependent temperature
    TEMP_BY_ROUND = {
        1: 1.10,
        2: 1.04,
        3: 0.96,
        4: 0.92,
        5: 0.90,
        6: 0.88,
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

    # --- 1) KenPom model probability with per-game strength shocks ---

    # Base ratings
    ra = getattr(team_a, "adj_em", None) if hasattr(team_a, "adj_em") else None
    rb = getattr(team_b, "adj_em", None) if hasattr(team_b, "adj_em") else None

    if ra is None or rb is None:
        ra = team_a.rating
        rb = team_b.rating
    if ra is None or rb is None:
        # last resort: seed-based pseudo-rating
        ra = float(17 - team_a.seed)
        rb = float(17 - team_b.seed)

    # Effective per-game strengths including performance shocks
    ra_eff = ra + strength_shock_a
    rb_eff = rb + strength_shock_b

    raw_gap = ra_eff - rb_eff

    # Non-linear compression: big gaps shrink, mid gaps stay meaningful
    gap = math.copysign(abs(raw_gap) ** 0.8, raw_gap)

    scale = SCALE_BY_ROUND.get(round_num, 4.5)
    p_model = logistic(gap, scale)
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
    # Region-level chaos: a shared logit-space shift for this (round, region).
    z_blend += region_noise
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

def _get_ordered_games(session: Session) -> list[TournamentGame]:
    games = session.query(TournamentGame).all()
    if not games:
        raise RuntimeError("No tournament games found. Load a bracket first.")

    # TournamentGame.slot encodes region as uppercase: EAST/WEST/SOUTH/MIDWEST.
    region_order: dict[str, int] = {"EAST": 0, "WEST": 1, "SOUTH": 2, "MIDWEST": 3}

    def bit_index_for_game(g: TournamentGame) -> int:
        slot = g.slot or ""
        # R64: EAST_R64_G1..EAST_R64_G8 etc.
        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_R64_G(\d+)$", slot)
        if m:
            reg = m.group(1)
            num = int(m.group(2))
            return 0 + region_order[reg] * 8 + (num - 1)

        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_R32_G(\d+)$", slot)
        if m:
            reg = m.group(1)
            num = int(m.group(2))
            return 32 + region_order[reg] * 4 + (num - 1)

        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_S16_G(\d+)$", slot)
        if m:
            reg = m.group(1)
            num = int(m.group(2))
            return 48 + region_order[reg] * 2 + (num - 1)

        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_E8$", slot)
        if m:
            reg = m.group(1)
            return 56 + region_order[reg]

        if slot == "FF_SEMI_1":
            return 60
        if slot == "FF_SEMI_2":
            return 61
        if slot == "NATIONAL_CHAMPIONSHIP":
            return 62

        raise ValueError(f"Unrecognized game slot format for bit order: {slot}")

    games_sorted = sorted(games, key=bit_index_for_game)
    indices = [bit_index_for_game(g) for g in games_sorted]
    if len(indices) != 63:
        raise ValueError(f"Expected 63 tournament games, got {len(indices)}")
    if sorted(indices) != list(range(63)):
        raise ValueError("Tournament game slots do not map to a contiguous 0..62 bit order")
    return games_sorted


def _build_team_fast(teams_by_id: Dict[int, Team]) -> _TeamFast:
    max_id = max(teams_by_id.keys()) if teams_by_id else 0
    strength_base = [0.0] * (max_id + 1)
    seed = [0] * (max_id + 1)
    kenpom_rank = [-1.0] * (max_id + 1)

    for tid, t in teams_by_id.items():
        seed[tid] = int(t.seed)
        rank = getattr(t, "kenpom_rank", None)
        kenpom_rank[tid] = float(rank) if rank is not None else -1.0

        adj_em = getattr(t, "adj_em", None)
        if adj_em is not None:
            strength_base[tid] = float(adj_em)
        else:
            rating = getattr(t, "rating", None)
            if rating is not None:
                strength_base[tid] = float(rating)
            else:
                # last resort fallback used by win_probability()
                strength_base[tid] = float(17 - t.seed)

    return _TeamFast(strength_base=strength_base, seed=seed, kenpom_rank=kenpom_rank)


def _build_game_specs(games: list[TournamentGame]) -> tuple[list[_GameSpec], int]:
    game_id_to_index = {g.id: i for i, g in enumerate(games)}

    # (round, region) => key index for region_noise
    noise_key_map: dict[tuple[int, str | None], int] = {}
    next_key_idx = 0

    specs: list[_GameSpec] = []
    for i, g in enumerate(games):
        key = (g.round, g.region)
        if key not in noise_key_map:
            noise_key_map[key] = next_key_idx
            next_key_idx += 1
        key_idx = noise_key_map[key]

        def _parse_team_source(src: str) -> tuple[int, int]:
            if src.startswith("TEAM-"):
                return 0, int(src.split("-", 1)[1])
            if src.startswith("WIN-"):
                upstream_gid = int(src.split("-", 1)[1])
                return 1, game_id_to_index[upstream_gid]
            raise ValueError(f"Unknown team source format: {src}")

        t1_kind, t1_val = _parse_team_source(g.team1_source)
        t2_kind, t2_val = _parse_team_source(g.team2_source)

        specs.append(
            _GameSpec(
                round_num=int(g.round),
                key_idx=key_idx,
                team1_kind=t1_kind,
                team1_val=t1_val,
                team2_kind=t2_kind,
                team2_val=t2_val,
            )
        )

    return specs, next_key_idx


def simulate_bracket_outcome_bits_fast(
    team_fast: _TeamFast,
    game_specs: list[_GameSpec],
    num_noise_keys: int,
    *,
    shock_sd_multiplier: float = 1.0,
    chaos_sd: float = 0.22,
) -> tuple[int, int]:
    num_games = len(game_specs)
    if num_games > 63:
        raise ValueError(f"Expected <= 63 tournament games, got {num_games}")

    winners: list[int] = [0] * num_games
    noise_shifts = [random.gauss(0.0, chaos_sd) for _ in range(num_noise_keys)]

    result_bits = 0
    for i, spec in enumerate(game_specs):
        t1_id = spec.team1_val if spec.team1_kind == 0 else winners[spec.team1_val]
        t2_id = spec.team2_val if spec.team2_kind == 0 else winners[spec.team2_val]

        sd = shock_sd_by_round(spec.round_num) * shock_sd_multiplier
        strength_shock_a = random.gauss(0.0, sd)
        strength_shock_b = random.gauss(0.0, sd)

        p = win_probability_fast(
            strength_base_a=team_fast.strength_base[t1_id],
            strength_base_b=team_fast.strength_base[t2_id],
            seed_a=team_fast.seed[t1_id],
            seed_b=team_fast.seed[t2_id],
            kenpom_rank_a=team_fast.kenpom_rank[t1_id],
            kenpom_rank_b=team_fast.kenpom_rank[t2_id],
            round_num=spec.round_num,
            region_noise=noise_shifts[spec.key_idx],
            strength_shock_a=strength_shock_a,
            strength_shock_b=strength_shock_b,
        )

        team1_won = random.random() < p
        winner_id = t1_id if team1_won else t2_id
        winners[i] = winner_id

        if team1_won:
            result_bits |= 1 << i

    champion_team_id = winners[num_games - 1]
    return result_bits, champion_team_id


def simulate_bracket_outcome_bits(
    teams_by_id: Dict[int, Team],
    games: list[TournamentGame],
    *,
    shock_sd_multiplier: float = 1.0,
    chaos_sd: float = 0.22,
) -> tuple[int, int]:
    """
    Simulate a full bracket, returning:
    - result_bits: packed 63-bit "team1 vs team2 won" outcomes
    - champion_team_id: the winning team id of the last game in `games`
    """
    num_games = len(games)
    if num_games > 63:
        raise ValueError(f"Expected <= 63 tournament games, got {num_games}")

    winners_by_key: Dict[str, int] = {}
    region_round_noise: Dict[tuple[int, str | None], float] = {}
    result_bits = 0

    for i, game in enumerate(games):
        key = (game.round, game.region)
        if key not in region_round_noise:
            region_round_noise[key] = random.gauss(0.0, chaos_sd)
        noise = region_round_noise[key]

        team1 = _resolve_team(game.team1_source, teams_by_id, winners_by_key)
        team2 = _resolve_team(game.team2_source, teams_by_id, winners_by_key)

        # Game-level performance shocks (in KenPom points)
        sd = shock_sd_by_round(game.round) * shock_sd_multiplier
        strength_shock_a = random.gauss(0.0, sd)
        strength_shock_b = random.gauss(0.0, sd)

        p = win_probability(
            team1,
            team2,
            round_num=game.round,
            region_noise=noise,
            strength_shock_a=strength_shock_a,
            strength_shock_b=strength_shock_b,
        )

        team1_won = random.random() < p
        winner = team1 if team1_won else team2

        if team1_won:
            result_bits |= 1 << i

        winners_by_key[f"WIN-{game.id}"] = winner.id

    champion_team_id = winners_by_key[f"WIN-{games[-1].id}"]
    return result_bits, champion_team_id


def decode_bracket_winners(
    result_bits: int,
    games: list[TournamentGame],
    teams_by_id: Dict[int, Team],
) -> dict[int, int]:
    """
    Decode packed `result_bits` into {game_id -> winner_team_id}.
    """
    winners_by_key: Dict[str, int] = {}
    winners_by_game_id: dict[int, int] = {}

    for i, game in enumerate(games):
        team1_won = ((result_bits >> i) & 1) == 1
        team1 = _resolve_team(game.team1_source, teams_by_id, winners_by_key)
        team2 = _resolve_team(game.team2_source, teams_by_id, winners_by_key)
        winner = team1 if team1_won else team2
        winners_by_key[f"WIN-{game.id}"] = winner.id
        winners_by_game_id[game.id] = winner.id

    return winners_by_game_id


def simulate_single_bracket(session: Session, model_version: str = "v1") -> int:
    """
    Simulate a single full tournament bracket and persist it.
    """
    teams_by_id = {t.id: t for t in session.query(Team).all()}
    games = _get_ordered_games(session)

    team_fast = _build_team_fast(teams_by_id)
    game_specs, num_noise_keys = _build_game_specs(games)
    result_bits, champion_team_id = simulate_bracket_outcome_bits_fast(
        team_fast, game_specs, num_noise_keys
    )

    bracket = Bracket(
        model_version=model_version,
        result_bits=result_bits,
        champion_team_id=champion_team_id,
        survival_index=len(games),
        created_at=datetime.utcnow(),
    )
    session.add(bracket)
    session.commit()
    return int(bracket.id)


def generate_brackets(
    session: Session,
    n: int,
    batch_size: int = 10_000,
    model_version: str = "v1",
    progress_callback: Callable[[int], None] | None = None,
) -> None:
    """
    Generate many brackets efficiently:
    - simulate in Python with pre-extracted numeric arrays
    - write using Postgres `COPY` (fast) when available
    - commit per `batch_size`
    """
    if n <= 0:
        return

    teams_by_id = {t.id: t for t in session.query(Team).all()}
    games = _get_ordered_games(session)
    team_fast = _build_team_fast(teams_by_id)
    game_specs, num_noise_keys = _build_game_specs(games)
    survival_initial = len(game_specs)  # e.g. 63 means "alive after all games"

    remaining = n
    generated = 0

    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    use_postgres_copy = dialect_name == "postgresql"

    while remaining > 0:
        current_batch = min(batch_size, remaining)
        remaining -= current_batch

        now = datetime.utcnow()

        if use_postgres_copy:
            # COPY streaming avoids per-row INSERT overhead across the network.
            created_at_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")
            mv = str(model_version).replace('"', '""')
            # Build CSV lines in memory for this batch.
            lines: list[str] = []
            for _ in range(current_batch):
                result_bits, champion_team_id = simulate_bracket_outcome_bits_fast(
                    team_fast, game_specs, num_noise_keys
                )
                lines.append(
                    f"{created_at_str},\"{mv}\",{result_bits},{champion_team_id},{survival_initial}\n"
                )
            csv_data = "".join(lines)

            sa_conn = session.connection()
            raw_conn = sa_conn.connection
            with raw_conn.cursor() as cur:
                cur.copy_expert(
                    "COPY brackets (created_at, model_version, result_bits, champion_team_id, survival_index) FROM STDIN WITH (FORMAT csv)",
                    io.StringIO(csv_data),
                )
        else:
            mappings: list[dict[str, object]] = []
            for _ in range(current_batch):
                result_bits, champion_team_id = simulate_bracket_outcome_bits_fast(
                    team_fast, game_specs, num_noise_keys
                )
                mappings.append(
                    {
                        "created_at": now,
                        "model_version": model_version,
                        "result_bits": result_bits,
                        "champion_team_id": champion_team_id,
                        "survival_index": survival_initial,
                    }
                )
            session.bulk_insert_mappings(Bracket, mappings)

        session.commit()
        session.expunge_all()

        generated += current_batch
        if progress_callback is not None:
            progress_callback(generated)

