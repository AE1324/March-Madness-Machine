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
    teams_by_id, games, played_winner_by_game_id, bracket_q = _load_simulation_context(session)

    # If no results have been entered yet, all brackets are perfect.
    if not played_winner_by_game_id:
        return int(bracket_q.count())

    perfect = 0
    for _, bits in bracket_q.yield_per(2000):
        assert bits is not None
        winners_by_game_id = decode_bracket_winners(int(bits), games, teams_by_id)
        is_perfect = True
        for gid, actual_winner_tid in played_winner_by_game_id.items():
            if winners_by_game_id.get(gid) != actual_winner_tid:
                is_perfect = False
                break
        if is_perfect:
            perfect += 1

    return perfect


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