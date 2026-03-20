from sqlalchemy.orm import Session

from models import Bracket, RealResult, Team, TournamentGame
from simulate import decode_bracket_winners


def _load_simulation_context(session: Session):
    teams_by_id = {t.id: t for t in session.query(Team).all()}
    games = (
        session.query(TournamentGame)
        .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
        .all()
    )
    if not games:
        raise RuntimeError("No tournament games found. Load a bracket first.")

    played_winner_by_game_id = {
        gid: wid
        for gid, wid in session.query(RealResult.game_id, RealResult.winner_team_id).all()
        if wid is not None
    }

    bracket_q = session.query(Bracket.id, Bracket.result_bits).filter(
        Bracket.result_bits.isnot(None)
    )
    return teams_by_id, games, played_winner_by_game_id, bracket_q


def count_perfect_brackets(session: Session) -> int:
    # Fast path using survival_index:
    # A bracket is "perfect" vs entered real results iff it survived through
    # the maximum entered game bit index.
    from sqlalchemy import func
    import re

    played_game_rows = (
        session.query(RealResult.game_id, RealResult.winner_team_id)
        .filter(RealResult.winner_team_id.isnot(None))
        .all()
    )

    total = (
        session.query(Bracket.id)
        .filter(Bracket.result_bits.isnot(None))
        .count()
    )
    if total == 0:
        return 0

    if not played_game_rows:
        return int(total)

    region_order = {"EAST": 0, "WEST": 1, "SOUTH": 2, "MIDWEST": 3}

    def bit_index_for_slot(slot: str) -> int:
        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_R64_G(\d+)$", slot)
        if m:
            return 0 + region_order[m.group(1)] * 8 + (int(m.group(2)) - 1)
        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_R32_G(\d+)$", slot)
        if m:
            return 32 + region_order[m.group(1)] * 4 + (int(m.group(2)) - 1)
        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_S16_G(\d+)$", slot)
        if m:
            return 48 + region_order[m.group(1)] * 2 + (int(m.group(2)) - 1)
        m = re.match(r"^(EAST|WEST|SOUTH|MIDWEST)_E8$", slot)
        if m:
            return 56 + region_order[m.group(1)]
        if slot == "FF_SEMI_1":
            return 60
        if slot == "FF_SEMI_2":
            return 61
        if slot == "NATIONAL_CHAMPIONSHIP":
            return 62
        raise ValueError(f"Unrecognized slot: {slot}")

    played_game_ids = [gid for gid, _ in played_game_rows]
    slots = (
        session.query(TournamentGame.id, TournamentGame.slot)
        .filter(TournamentGame.id.in_(played_game_ids))
        .all()
    )
    max_k = max(bit_index_for_slot(s.slot) for s in slots)

    # Survived through max_k <=> survival_index >= max_k.
    return int(
        session.query(func.count(Bracket.id))
        .filter(Bracket.result_bits.isnot(None))
        .filter(Bracket.survival_index >= max_k)
        .scalar()
    )


def leaderboard(session: Session, limit: int = 25):
    teams_by_id, games, played_winner_by_game_id, bracket_q = _load_simulation_context(session)

    decided = len(played_winner_by_game_id)
    scored_rows: list[dict[str, int]] = []

    for bid, bits in bracket_q.yield_per(2000):
        assert bits is not None
        winners_by_game_id = decode_bracket_winners(int(bits), games, teams_by_id)
        correct = 0
        for gid, actual_winner_tid in played_winner_by_game_id.items():
            if winners_by_game_id.get(gid) == actual_winner_tid:
                correct += 1
        scored_rows.append({"bracket_id": int(bid), "correct": correct, "decided": decided})

    scored_rows.sort(key=lambda r: (-r["correct"], -r["decided"], r["bracket_id"]))
    return scored_rows[:limit]


def pick_percentages_by_round(session: Session, round_num: int):
    teams_by_id, games, _played_winner_by_game_id, bracket_q = _load_simulation_context(session)

    bracket_count = bracket_q.count()
    if bracket_count == 0:
        return []

    games_in_round = [g for g in games if g.round == round_num]
    pick_counts: dict[int, int] = {}

    for _, bits in bracket_q.yield_per(2000):
        assert bits is not None
        winners_by_game_id = decode_bracket_winners(int(bits), games, teams_by_id)
        for g in games_in_round:
            win_tid = winners_by_game_id[g.id]
            pick_counts[win_tid] = pick_counts.get(win_tid, 0) + 1

    rows: list[dict[str, object]] = []
    for team_id, cnt in pick_counts.items():
        t = teams_by_id[team_id]
        rows.append(
            {
                "name": t.name,
                "seed": t.seed,
                "region": t.region,
                "picks": cnt,
                "pct": cnt / float(bracket_count),
            }
        )

    rows.sort(key=lambda r: (-float(r["pct"]), int(r["seed"]), str(r["name"])))
    return rows