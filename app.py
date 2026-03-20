import os
import re
from io import StringIO

import zipfile
from io import BytesIO

import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from db import engine as default_engine
from models import Team, TournamentGame, Bracket, RealResult
from simulate import generate_brackets, decode_bracket_winners
from view_bracket import main as view_bracket_main  # we'll re-use its logic indirectly
from main import ensure_bracket_loaded

from sqlalchemy import text
from stats import count_perfect_brackets, leaderboard, pick_percentages_by_round


# --- DB helpers ---

def get_engine():
    url = os.getenv("DATABASE_URL") or st.secrets.get("DATABASE_URL", None)
    if url:
        return create_engine(url, future=True)
    return default_engine


def get_latest_brackets(limit: int = 20):
    engine = get_engine()
    with Session(engine) as session:
        return (
            session.query(Bracket)
            .order_by(Bracket.id.desc())
            .limit(limit)
            .all()
        )


def bracket_exists(bracket_id: int) -> bool:
    engine = get_engine()
    with Session(engine) as session:
        return session.query(Bracket).filter(Bracket.id == bracket_id).count() > 0


def export_bracket_text(bracket_id: int) -> str:
    """
    Call into the view_bracket logic in-process and capture its text output.
    We re-import and run its main rendering function with a small shim.
    """
    # We'll temporarily monkeypatch argparse in view_bracket by calling it as a module
    # But easier: reimplement the minimal part here using the DB directly.

    engine = get_engine()
    with Session(engine) as session:
        bracket = session.query(Bracket).filter(Bracket.id == bracket_id).one_or_none()
        if bracket is None:
            return f"No bracket found for bracket_id={bracket_id}\n"

        if bracket.result_bits is None:
            return (
                f"Bracket {bracket_id} has no result_bits. Regenerate with the new simulator.\n"
            )

        teams = session.query(Team).all()
        teams_by_id = {t.id: t for t in teams}

        games = (
            session.query(TournamentGame)
            .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
            .all()
        )

        winner_by_game_id = decode_bracket_winners(
            int(bracket.result_bits), games, teams_by_id
        )

        # We'll reuse the same text format as view_bracket: simple region-wise summary
        from view_bracket import REGIONS, ROUND_LABEL  # type: ignore

        def seeded_name(team_id: int) -> str:
            t = teams_by_id[team_id]
            return f"({t.seed}) {t.name}"

        def resolve_team_id(source: str) -> int:
            if source.startswith("TEAM-"):
                return int(source.split("-", 1)[1])
            if source.startswith("WIN-"):
                gid = int(source.split("-", 1)[1])
                return winner_by_game_id[gid]
            raise ValueError(f"Unknown source format: {source}")

        out: list[str] = []
        out.append("MARCH MADNESS BRACKET (64 TEAMS)")
        out.append(f"BRACKET ID: {bracket_id}")
        out.append("")

        # group games by region+round
        by_region_round: dict[str, dict[int, list[TournamentGame]]] = {r: {} for r in REGIONS}
        finals = []
        champs = []

        for g in games:
            if g.region in REGIONS and 1 <= g.round <= 4:
                by_region_round[g.region].setdefault(g.round, []).append(g)
            elif g.round == 5:
                finals.append(g)
            elif g.round == 6:
                champs.append(g)

        for region in ["East", "West", "South", "Midwest"]:
            out.append(f"{region.upper()} REGION")
            for rnd in (1, 2, 3, 4):
                label = ROUND_LABEL[rnd]
                out.append(label)
                for g in by_region_round[region].get(rnd, []):
                    t1 = seeded_name(resolve_team_id(g.team1_source))
                    t2 = seeded_name(resolve_team_id(g.team2_source))
                    out.append(f"{t1} vs {t2}")
                out.append("")
            out.append("")

        out.append("FINAL FOUR")
        for g in sorted(finals, key=lambda x: x.slot):
            t1 = seeded_name(resolve_team_id(g.team1_source))
            t2 = seeded_name(resolve_team_id(g.team2_source))
            out.append(f"{t1} vs {t2}")
        out.append("")
        out.append("NATIONAL CHAMPIONSHIP")
        if champs:
            g = champs[0]
            t1 = seeded_name(resolve_team_id(g.team1_source))
            t2 = seeded_name(resolve_team_id(g.team2_source))
            out.append(f"{t1} vs {t2}")
        out.append("")
        if champs:
            champ_team = teams_by_id[winner_by_game_id[champs[0].id]]
            out.append(f"NATIONAL CHAMPION ({champ_team.seed}) {champ_team.name}")
        out.append("")
        return "\n".join(out)


def _ensure_derived_tables_exist(engine) -> None:
    """
    Denormalized helper tables for the UI.
    These are not represented as ORM models, so we create them on demand.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS pick_stats (
                    round INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    picks BIGINT NOT NULL,
                    pct DOUBLE PRECISION NOT NULL
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS brackets_at_risk (
                    round INTEGER NOT NULL,
                    region TEXT,
                    slot TEXT NOT NULL,
                    team_id INTEGER NOT NULL,
                    brackets_needing BIGINT NOT NULL,
                    pct_among_perfect DOUBLE PRECISION NOT NULL
                );
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS game_survival (
                    game_index INTEGER NOT NULL,
                    game_id INTEGER NOT NULL,
                    round INTEGER NOT NULL,
                    region TEXT,
                    slot TEXT NOT NULL,
                    alive_brackets BIGINT NOT NULL,
                    died_at_index BIGINT NOT NULL,
                    alive_pct DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (game_index)
                );
                """
            )
        )


def recompute_pick_stats_and_brackets_at_risk(
    engine,
    *,
    progress_callback=None,
) -> None:
    """
    Recompute `pick_stats` and `brackets_at_risk` using `brackets.result_bits`.
    """
    _ensure_derived_tables_exist(engine)

    with Session(engine) as session:
        teams_by_id = {t.id: t for t in session.query(Team).all()}
        # IMPORTANT: decode assumes games are in packed-bit order (0..62).
        games = _ordered_games_by_bit_index(session)
        if not games:
            raise RuntimeError("No tournament games found. Load a bracket first.")

        played_rows = session.query(RealResult.game_id, RealResult.winner_team_id).all()
        played_winner_by_game_id = {
            gid: wid for gid, wid in played_rows if wid is not None
        }
        result_game_ids = {gid for gid, _ in played_rows}
        future_game_indices = [i for i, g in enumerate(games) if g.id not in result_game_ids]

        bracket_q = session.query(Bracket.id, Bracket.result_bits).filter(
            Bracket.result_bits.isnot(None)
        )
        bracket_count = bracket_q.count()
        if bracket_count == 0:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM pick_stats;"))
                conn.execute(text("DELETE FROM brackets_at_risk;"))
            return

        from collections import defaultdict

        pick_counts: dict[tuple[int, int], int] = defaultdict(int)
        risk_counts: dict[tuple[int, str | None, str, int], int] = defaultdict(int)
        perfect_brackets = 0
        processed = 0

        # Precompile a fast decoder (no per-bracket string parsing/dict resolution).
        game_id_to_index = {g.id: i for i, g in enumerate(games)}
        t1_is_team: list[bool] = []
        t1_val: list[int] = []
        t2_is_team: list[bool] = []
        t2_val: list[int] = []
        rounds: list[int] = []
        regions: list[str | None] = []
        slots: list[str] = []

        for g in games:
            rounds.append(int(g.round))
            regions.append(g.region)
            slots.append(str(g.slot))

            def _compile_source(src: str) -> tuple[bool, int]:
                if src.startswith("TEAM-"):
                    return True, int(src.split("-", 1)[1])
                if src.startswith("WIN-"):
                    upstream_gid = int(src.split("-", 1)[1])
                    return False, int(game_id_to_index[upstream_gid])
                raise ValueError(f"Unrecognized team source: {src}")

            is_team_1, v1 = _compile_source(str(g.team1_source))
            is_team_2, v2 = _compile_source(str(g.team2_source))
            t1_is_team.append(is_team_1)
            t1_val.append(v1)
            t2_is_team.append(is_team_2)
            t2_val.append(v2)

        played_checks: list[tuple[int, int]] = []
        for gid, wid in played_winner_by_game_id.items():
            idx = game_id_to_index.get(gid)
            if idx is not None:
                played_checks.append((idx, int(wid)))

        def _decode_winners_list(bits_int: int) -> list[int]:
            winners: list[int] = [0] * len(games)
            for i in range(len(games)):
                a = t1_val[i] if t1_is_team[i] else winners[t1_val[i]]
                b = t2_val[i] if t2_is_team[i] else winners[t2_val[i]]
                team1_won = ((bits_int >> i) & 1) == 1
                winners[i] = a if team1_won else b
            return winners

        if progress_callback is not None:
            progress_callback(0, bracket_count)

        for _, bits in bracket_q.yield_per(2000):
            assert bits is not None
            winners = _decode_winners_list(int(bits))

            # Pick distributions by round (all brackets).
            for i in range(len(games)):
                pick_counts[(rounds[i], winners[i])] += 1

            # Perfect bracket check (played games only).
            is_perfect = True
            for idx, actual_winner_tid in played_checks:
                if winners[idx] != actual_winner_tid:
                    is_perfect = False
                    break

            if is_perfect:
                perfect_brackets += 1
                for j in future_game_indices:
                    rkey = (rounds[j], regions[j], slots[j], winners[j])
                    risk_counts[rkey] += 1

            processed += 1
            if progress_callback is not None and (processed % 2000 == 0):
                progress_callback(processed, bracket_count)

        if progress_callback is not None:
            progress_callback(bracket_count, bracket_count)

        pick_rows: list[dict[str, object]] = []
        for (rnd, team_id), cnt in pick_counts.items():
            pick_rows.append(
                {
                    "round": rnd,
                    "team_id": team_id,
                    "picks": cnt,
                    "pct": cnt / float(bracket_count),
                }
            )

        risk_rows: list[dict[str, object]] = []
        denom = float(perfect_brackets) if perfect_brackets > 0 else 1.0
        for (rnd, region, slot, team_id), cnt in risk_counts.items():
            risk_rows.append(
                {
                    "round": rnd,
                    "region": region,
                    "slot": slot,
                    "team_id": team_id,
                    "brackets_needing": cnt,
                    "pct_among_perfect": cnt / denom,
                }
            )

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM pick_stats;"))
            conn.execute(text("DELETE FROM brackets_at_risk;"))

            if pick_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO pick_stats (round, team_id, picks, pct)
                        VALUES (:round, :team_id, :picks, :pct)
                        """
                    ),
                    pick_rows,
                )
            if risk_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO brackets_at_risk (
                            round, region, slot, team_id, brackets_needing, pct_among_perfect
                        )
                        VALUES (
                            :round, :region, :slot, :team_id, :brackets_needing, :pct_among_perfect
                        )
                        """
                    ),
                    risk_rows,
                )


def _bit_index_for_slot(slot: str) -> int:
    """
    Map TournamentGame.slot (from `MM_2026.json`) into the packed `result_bits` bit index:
    0-31: R64, 32-47: R32, 48-55: S16, 56-59: E8, 60-61: Final Four, 62: Championship.
    """
    region_order = {"EAST": 0, "WEST": 1, "SOUTH": 2, "MIDWEST": 3}

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

    raise ValueError(f"Unrecognized slot for bit order: {slot}")


def _ordered_games_by_bit_index(session: Session) -> list[TournamentGame]:
    games = session.query(TournamentGame).all()
    if not games:
        raise RuntimeError("No tournament games found. Load the bracket first.")

    games_sorted = sorted(games, key=lambda g: _bit_index_for_slot(g.slot))
    indices = [_bit_index_for_slot(g.slot) for g in games_sorted]

    if len(indices) != 63 or sorted(indices) != list(range(63)):
        raise RuntimeError("TournamentGame slots do not map to a contiguous 0..62 bit index.")
    return games_sorted


def _apply_survival_update_for_game(
    session: Session,
    *,
    game_index: int,
    true_bit: int,
) -> int:
    """
    One-step scalable elimination update for game bit index `game_index`.
    Sets survival_index := min(survival_index, game_index - 1) for incorrect brackets.
    """
    # Clamp: first game index (0) kills to -1.
    new_survival = game_index - 1
    result = session.execute(
        text(
            """
            UPDATE brackets
            SET survival_index = LEAST(survival_index, :new_survival)
            WHERE survival_index >= :game_index
              AND ((result_bits >> :game_index) & 1) != :true_bit;
            """
        ),
        {"new_survival": new_survival, "game_index": game_index, "true_bit": true_bit},
    )

    # Number of brackets whose survival_index was updated for this game.
    # For Postgres this should reflect the number of eliminated (incorrect) brackets.
    return int(result.rowcount or 0)


def _apply_survival_update_for_game_with_progress(
    session: Session,
    *,
    game_index: int,
    true_bit: int,
    progress_callback=None,
    id_window: int = 200_000,
) -> tuple[int, int]:
    """
    Chunked survival update with progress reporting.

    Returns:
      (updated_count, total_candidates_checked)
    """
    total_candidates = int(
        session.execute(
            text(
                """
                SELECT count(*)
                FROM brackets
                WHERE result_bits IS NOT NULL
                  AND survival_index >= :game_index;
                """
            ),
            {"game_index": game_index},
        ).scalar_one()
    )
    if total_candidates == 0:
        if progress_callback is not None:
            progress_callback(0, 0, 0)
        return 0, 0

    min_max = session.execute(
        text(
            """
            SELECT min(id), max(id)
            FROM brackets
            WHERE result_bits IS NOT NULL
              AND survival_index >= :game_index;
            """
        ),
        {"game_index": game_index},
    ).one()
    min_id = int(min_max[0])
    max_id = int(min_max[1])

    checked = 0
    updated_total = 0
    new_survival = game_index - 1

    lo = min_id
    while lo <= max_id:
        hi = lo + id_window - 1

        in_window = int(
            session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM brackets
                    WHERE id BETWEEN :lo AND :hi
                      AND result_bits IS NOT NULL
                      AND survival_index >= :game_index;
                    """
                ),
                {"lo": lo, "hi": hi, "game_index": game_index},
            ).scalar_one()
        )

        if in_window > 0:
            upd = session.execute(
                text(
                    """
                    UPDATE brackets
                    SET survival_index = LEAST(survival_index, :new_survival)
                    WHERE id BETWEEN :lo AND :hi
                      AND result_bits IS NOT NULL
                      AND survival_index >= :game_index
                      AND ((result_bits >> :game_index) & 1) != :true_bit;
                    """
                ),
                {
                    "new_survival": new_survival,
                    "lo": lo,
                    "hi": hi,
                    "game_index": game_index,
                    "true_bit": true_bit,
                },
            )
            updated_total += int(upd.rowcount or 0)
            checked += in_window
            if progress_callback is not None:
                progress_callback(checked, total_candidates, updated_total)

        lo = hi + 1

    return updated_total, total_candidates


def rebuild_survival_from_real_results(engine) -> None:
    """
    Reset survival_index and re-apply elimination updates in official bit order,
    using all currently-entered `real_results`.
    """
    _ensure_derived_tables_exist(engine)

    with Session(engine) as session:
        games_sorted = _ordered_games_by_bit_index(session)

        played = session.query(RealResult.game_id, RealResult.winner_team_id).all()
        winners_by_game_id = {gid: wid for gid, wid in played if wid is not None}

        # Reset all brackets to "alive" (survival_index = 63).
        session.execute(text("UPDATE brackets SET survival_index = 63;"))
        session.flush()

        for g in games_sorted:
            actual_winner_tid = winners_by_game_id.get(g.id)
            if actual_winner_tid is None:
                continue

            # Resolve the team that was on the TEAM-A side for this game,
            # using actual upstream winners.
            def resolve_team_id(source: str) -> int | None:
                if source.startswith("TEAM-"):
                    return int(source.split("-", 1)[1])
                if source.startswith("WIN-"):
                    upstream_gid = int(source.split("-", 1)[1])
                    return winners_by_game_id.get(upstream_gid)
                return None

            team1_tid = resolve_team_id(g.team1_source)
            if team1_tid is None:
                continue

            true_bit = 1 if actual_winner_tid == team1_tid else 0
            k = _bit_index_for_slot(g.slot)
            _apply_survival_update_for_game(session, game_index=k, true_bit=true_bit)

        session.commit()


def rebuild_game_survival_from_survival_index(engine) -> None:
    """
    Populate `game_survival` from `brackets.survival_index` distribution.
    No decoding of `result_bits` required.
    """
    _ensure_derived_tables_exist(engine)

    with Session(engine) as session:
        games_sorted = _ordered_games_by_bit_index(session)

        from sqlalchemy import func

        total = (
            session.query(Bracket.id)
            .filter(Bracket.result_bits.isnot(None))
            .count()
        )
        if total == 0:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM game_survival;"))
            return

        dist = (
            session.query(Bracket.survival_index, func.count(Bracket.id))
            .filter(Bracket.result_bits.isnot(None))
            .group_by(Bracket.survival_index)
            .all()
        )
        count_map = {int(si): int(cnt) for si, cnt in dist}

        # died_at_index[i] = number of brackets dying at game_index i
        # which corresponds to survival_index == (i - 1).
        died_at_index = [count_map.get(i - 1, 0) for i in range(63)]

        alive_running = total
        rows: list[dict[str, object]] = []
        for i, g in enumerate(games_sorted):
            # alive after game_index i = total - sum_{j<=i} died_at_index[j]
            alive_running -= died_at_index[i]
            rows.append(
                {
                    "game_index": i,
                    "game_id": g.id,
                    "round": g.round,
                    "region": g.region,
                    "slot": g.slot,
                    "alive_brackets": alive_running,
                    "died_at_index": died_at_index[i],
                    "alive_pct": alive_running / float(total),
                }
            )

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM game_survival;"))
            if rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO game_survival (
                            game_index, game_id, round, region, slot,
                            alive_brackets, died_at_index, alive_pct
                        ) VALUES (
                            :game_index, :game_id, :round, :region, :slot,
                            :alive_brackets, :died_at_index, :alive_pct
                        );
                        """
                    ),
                    rows,
                )


def _ensure_game_survival_initialized(engine, session: Session) -> int:
    """
    Ensure `game_survival` is populated.

    When no real results have been entered yet, all brackets should still have
    survival_index = 63, so the initial survival curve is uniform:
      alive_brackets(game_index=i) = total_brackets for all i in [0..62].

    Returns the total bracket count used for alive_pct.
    """
    existing = session.execute(text("SELECT count(*) FROM game_survival;")).scalar_one()
    if existing and existing > 0:
        total = session.execute(
            text("SELECT alive_brackets FROM game_survival WHERE game_index = 0;")
        ).scalar_one()
        return int(total)

    total = (
        session.query(Bracket.id)
        .filter(Bracket.result_bits.isnot(None))
        .count()
    )
    if total == 0:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM game_survival;"))
        return 0

    games_sorted = _ordered_games_by_bit_index(session)
    rows: list[dict[str, object]] = []
    for i, g in enumerate(games_sorted):
        rows.append(
            {
                "game_index": i,
                "game_id": g.id,
                "round": g.round,
                "region": g.region,
                "slot": g.slot,
                "alive_brackets": total,
                "died_at_index": 0,
                "alive_pct": 1.0,
            }
        )

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM game_survival;"))
        conn.execute(
            text(
                """
                INSERT INTO game_survival (
                    game_index, game_id, round, region, slot,
                    alive_brackets, died_at_index, alive_pct
                ) VALUES (
                    :game_index, :game_id, :round, :region, :slot,
                    :alive_brackets, :died_at_index, :alive_pct
                );
                """
            ),
            rows,
        )
    return int(total)


def _apply_game_survival_incremental_update(
    engine,
    session: Session,
    *,
    game_index: int,
    delta_died_at_index: int,
    total_brackets: int,
) -> None:
    """
    Incrementally update `game_survival` after applying a survival_index update
    for a single game bit index.

    If `delta_died_at_index` brackets die at game_index=k, then:
    - died_at_index(k) increases by delta
    - alive_brackets(i) decreases by delta for all i >= k
    - alive_pct is updated accordingly
    """
    if delta_died_at_index <= 0 or total_brackets <= 0:
        return

    session.execute(
        text(
            """
            UPDATE game_survival
            SET
              alive_brackets = alive_brackets - :delta,
              alive_pct = (alive_brackets - :delta)::double precision / :total
            WHERE game_index >= :k;
            """
        ),
        {"delta": delta_died_at_index, "total": total_brackets, "k": game_index},
    )
    session.execute(
        text(
            """
            UPDATE game_survival
            SET died_at_index = died_at_index + :delta
            WHERE game_index = :k;
            """
        ),
        {"delta": delta_died_at_index, "k": game_index},
    )


def recompute_game_survival(engine) -> None:
    # Backwards-compatible name: rebuild the survival curve from survival_index.
    rebuild_game_survival_from_survival_index(engine)


# --- Streamlit UI ---

st.set_page_config(page_title="Bracket Simulator", layout="wide")

try:
    ensure_bracket_loaded("MM_2026.json")
    engine0 = get_engine()
    _ensure_derived_tables_exist(engine0)

    # If real results already exist (e.g. after a restart) but the survival table
    # is empty, rebuild once so the Stats tab shows the correct curve.
    with Session(engine0) as _s:
        real_played = (
            _s.query(RealResult.game_id)
            .filter(RealResult.winner_team_id.isnot(None))
            .limit(1)
            .all()
        )
        survival_rows = _s.execute(text("SELECT count(*) FROM game_survival;")).scalar_one()
        any_brackets = _s.query(Bracket.id).filter(Bracket.result_bits.isnot(None)).limit(1).all()

    if any_brackets and survival_rows == 0:
        if real_played:
            rebuild_survival_from_real_results(engine0)
        rebuild_game_survival_from_survival_index(engine0)
except Exception as e:
    st.error("Database is not reachable. Start the Postgres container, then refresh the app.")
    st.code('docker start mm-postgres\n# or create it:\ndocker run --name mm-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=brackets -p 5432:5432 -d postgres:18')
    st.exception(e)
    st.stop()

st.title("March Madness Bracket Simulator")

st.caption("Generate and view AI-driven brackets using KenPom-based probabilities.")

col_left, col_right = st.columns(2)
tab_results, tab_stats, tab_admin = st.tabs(["Enter Results", "Stats", "Admin"])

with tab_results:
    st.subheader("Enter real results")

    engine = get_engine()
    with Session(engine) as session:
        games = (
            session.query(TournamentGame)
            .order_by(TournamentGame.round, TournamentGame.id)
            .all()
        )
        teams = {t.id: t for t in session.query(Team).all()}

        if not games:
            st.warning("No tournament games found. Load the bracket first.")
        else:
            # Pick a game
            game_labels = {
                f"R{g.round} {g.region or 'FF'} {g.slot} (game_id={g.id})": g
                for g in games
            }
            sel_label = st.selectbox("Game", list(game_labels.keys()))
            game = game_labels[sel_label]

            # Helper to resolve team IDs for this game
            def resolve_team_id(source: str) -> int:
                if source.startswith("TEAM-"):
                    return int(source.split("-", 1)[1])
                if source.startswith("WIN-"):
                    gid = int(source.split("-", 1)[1])
                    winner = (
                        session.query(RealResult)
                        .filter(RealResult.game_id == gid)
                        .one_or_none()
                    )
                    if not winner:
                        raise ValueError(f"Upstream game {gid} has no result yet.")
                    return winner.winner_team_id
                raise ValueError(f"Unknown source: {source}")

            try:
                t1_id = resolve_team_id(game.team1_source)
                t2_id = resolve_team_id(game.team2_source)
                t1 = teams[t1_id]
                t2 = teams[t2_id]
            except Exception as e:
                st.error(str(e))
                st.stop()

            st.write(f"**Matchup:** R{game.round} {game.region or 'FF'} {game.slot}")
            col_a, col_b = st.columns(2)

            def save_winner(winner_id: int):
                prev_winner_id = (
                    session.query(RealResult.winner_team_id)
                    .filter(RealResult.game_id == game.id)
                    .one_or_none()
                )
                prev_winner_val = (
                    int(prev_winner_id[0]) if prev_winner_id is not None else None
                )

                session.execute(
                    text("""
                    insert into real_results (game_id, winner_team_id, loser_team_id)
                    values (:gid, :wid, null)
                    on conflict (game_id)
                    do update set winner_team_id = excluded.winner_team_id;
                    """),
                    {"gid": game.id, "wid": winner_id},
                )
                session.commit()

                # Update packed-bracket survival incrementally.
                # If the winner changed, we must rebuild because survival_index only decreases.
                k = _bit_index_for_slot(game.slot)
                true_bit = 1 if winner_id == t1_id else 0

                if prev_winner_val is not None and prev_winner_val != int(winner_id):
                    rebuild_survival_from_real_results(engine)
                else:
                    progress = st.progress(0)
                    status = st.empty()
                    import time
                    t0 = time.time()

                    def _progress_cb(done: int, total: int, eliminated: int) -> None:
                        pct = int((done / max(total, 1)) * 100)
                        progress.progress(min(100, max(pct, 0)))
                        elapsed = time.time() - t0
                        left = max(total - done, 0)
                        status.text(
                            f"Checked {done:,}/{total:,} brackets ({pct}%) | "
                            f"remaining {left:,} | eliminated {eliminated:,} | "
                            f"{elapsed:0.1f}s elapsed"
                        )

                    # Speed up heavy elimination updates on large tables.
                    session.execute(text("SET LOCAL synchronous_commit = OFF;"))
                    delta, _checked = _apply_survival_update_for_game_with_progress(
                        session,
                        game_index=k,
                        true_bit=true_bit,
                        progress_callback=_progress_cb,
                    )
                    session.commit()
                    total = _ensure_game_survival_initialized(engine, session)
                    _apply_game_survival_incremental_update(
                        engine,
                        session,
                        game_index=k,
                        delta_died_at_index=delta,
                        total_brackets=total,
                    )
                    session.commit()
                    status.text("")
                    st.success("Saved result.")
                    return

                # Winner changed: survival_index only decreases, so we must fully
                # rebuild to keep `game_survival` consistent.
                rebuild_game_survival_from_survival_index(engine)
                st.success("Saved result.")

            with col_a:
                if st.button(f"Winner: ({t1.seed}) {t1.name}", key=f"win_{game.id}_t1"):
                    save_winner(t1_id)

            with col_b:
                if st.button(f"Winner: ({t2.seed}) {t2.name}", key=f"win_{game.id}_t2"):
                    save_winner(t2_id)


with tab_stats:
    st.subheader("Tournament stats")

    engine = get_engine()
    with Session(engine) as session:
        st.metric("Perfect brackets remaining", count_perfect_brackets(session))

    # Survival curve based on packed results + entered real results.
    engine = get_engine()
    with Session(engine) as session:
        from sqlalchemy import text as _text

        rows = session.execute(
            _text(
                """
                SELECT game_index, alive_brackets, alive_pct
                FROM game_survival
                ORDER BY game_index ASC;
                """
            )
        ).mappings().all()

    if rows:
        st.subheader("Perfect bracket survival (by game index)")
        if rows:
            st.metric(
                "Survival after all 63 games",
                f"{rows[-1]['alive_pct']*100:.4f}%",
            )
        st.line_chart(
            {
                "alive_brackets": [r["alive_brackets"] for r in rows],
                "alive_pct": [r["alive_pct"] for r in rows],
            }
        )
    else:
        st.info(
            "No `game_survival` data yet. Click Admin → "
            "“Recompute pick percentages and brackets at risk (all brackets)” "
            "to generate the survival curve."
        )

        st.subheader("Leaderboard (most correct picks so far)")
        st.dataframe(leaderboard(session, limit=50), use_container_width=True)

    
    st.subheader("Pick percentages by round")

    rnd = st.selectbox("Round", [1, 2, 3, 4, 5, 6], index=0)

    engine = get_engine()
    with Session(engine) as session:
        from sqlalchemy import text
        rows = session.execute(
            text("""
            SELECT
              t.name,
              t.seed,
              t.region,
              ps.picks,
              ps.pct
            FROM pick_stats ps
            JOIN teams t ON t.id = ps.team_id
            WHERE ps.round = :rnd
            ORDER BY ps.pct DESC, t.seed ASC, t.name ASC;
            """),
            {"rnd": rnd},
        ).mappings().all()

    st.dataframe(rows, use_container_width=True)
    st.caption("Update via Admin → Recompute pick percentages.")


    st.subheader("Brackets at risk (perfect brackets only, precomputed)")

    engine = get_engine()
    with Session(engine) as session:
        from sqlalchemy import text
        rows = session.execute(
            text("""
            SELECT
                'R' || bar.round || ' ' || COALESCE(bar.region, 'FF') || ' ' || bar.slot AS game,
                t.seed,
                t.name AS team_name,
                bar.brackets_needing,
                bar.pct_among_perfect,
                bar.round,
                bar.region,
                bar.slot
            FROM brackets_at_risk bar
            JOIN teams t ON t.id = bar.team_id
            ORDER BY
                bar.round,
                bar.region NULLS LAST,
                bar.slot,
                t.seed;  -- ensures exactly 2 rows per game, in seed order
            """)
        ).mappings().all()

    if rows:
        st.dataframe(
            rows,
            use_container_width=True,
            column_order=[
                "game",
                "team_name",
                "seed",
                "brackets_needing",
                "pct_among_perfect",
                "round",
                "region",
                "slot",
            ],
        )
        st.caption("Updated via Admin → Recompute pick percentages and brackets at risk.")
    else:
        st.info("No data yet. Use Admin to recompute stats.")
    

with tab_admin:
    st.subheader("Admin")

    st.subheader("Reset data")

    clear_brackets = st.checkbox("Confirm: delete ALL generated brackets and picks")
    if st.button("Delete brackets & picks", disabled=not clear_brackets):
        engine = get_engine()
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE bracket_picks, brackets RESTART IDENTITY CASCADE;"
                )
            )
            # Also clear derived UI tables so the app doesn't show stale stats.
            conn.execute(text("TRUNCATE TABLE pick_stats, brackets_at_risk, game_survival;"))
        st.success("All brackets and picks deleted.")

    clear_results = st.checkbox("Confirm: delete ALL entered real results")
    if st.button("Delete real results", disabled=not clear_results):
        engine = get_engine()
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE real_results RESTART IDENTITY CASCADE;"))
            # Reset bracket survival tracking since it depends on real results.
            conn.execute(text("UPDATE brackets SET survival_index = 63;"))
            conn.execute(text("TRUNCATE TABLE game_survival;"))
        st.success("All real results deleted.")


    
    st.subheader("Pick stats")
    
    if st.button("Recompute pick percentages and brackets at risk (all brackets)"):
        engine = get_engine()
        progress = st.progress(0)
        status = st.empty()
        import time
        t0 = time.time()

        def _cb(done: int, total: int) -> None:
            pct = int((done / max(total, 1)) * 100)
            progress.progress(min(100, max(pct, 0)))
            elapsed = time.time() - t0
            status.text(f"Processed {done:,} / {total:,} brackets ({pct}%) — {elapsed:0.1f}s elapsed")

        with st.spinner("Recomputing pick stats from packed bracket outcomes..."):
            recompute_pick_stats_and_brackets_at_risk(engine, progress_callback=_cb)
        status.text("")
        st.success("Pick stats and brackets-at-risk recomputed.")

with col_left:
    st.subheader("Generate brackets")

    n = st.number_input(
        "How many brackets to generate?",
        min_value=1,
        max_value=1_000_000,
        value=1,
        step=1,
    )
    

    if st.button("Generate", type="primary"):
        engine = get_engine()
        total = int(n)
        progress = st.progress(0)
        status = st.empty()
        with Session(engine) as session:
            def _cb(so_far: int) -> None:
                pct = int((so_far / max(total, 1)) * 100)
                progress.progress(min(100, max(pct, 0)))
                status.text(f"Generated {so_far} of {total} brackets...")

            generate_brackets(
                session,
                n=total,
                batch_size=10_000,
                progress_callback=_cb,
            )

            # Best-effort "latest id" for the UI; IDs are monotonic.
            latest_id = (
                session.query(Bracket.id)
                .order_by(Bracket.id.desc())
                .limit(1)
                .scalar()
            )

        # Refresh survival curve derived table so the Stats chart reflects the
        # newly generated bracket population immediately.
        rebuild_game_survival_from_survival_index(engine)

        if latest_id is not None:
            status.text("")
            st.success(f"Generated {total} bracket(s). Latest ID: {int(latest_id)}")
            st.session_state["last_bracket_id"] = int(latest_id)

    st.divider()
    st.subheader("Generate + download (.zip)")

    zip_n = st.number_input(
        "How many brackets to generate and download as .txt files?",
        min_value=1,
        max_value=5000,
        value=50,
        step=1,
    )

    if st.button("Generate & download ZIP"):
        engine = get_engine()
        zip_n_int = int(zip_n)
        progress = st.progress(0)
        status = st.empty()
        with Session(engine) as session:
            start_id = session.query(Bracket.id).order_by(Bracket.id.desc()).limit(1).scalar()
            start_id = int(start_id) if start_id is not None else 0

            def _cb(so_far: int) -> None:
                pct = int((so_far / max(zip_n_int, 1)) * 100)
                progress.progress(min(100, max(pct, 0)))
                status.text(f"Generated {so_far} of {zip_n_int} brackets...")

            generate_brackets(
                session,
                n=zip_n_int,
                batch_size=2000,
                progress_callback=_cb,
            )

            status.text("")
            rows = (
                session.query(Bracket.id)
                .filter(Bracket.id > start_id)
                .order_by(Bracket.id.asc())
                .all()
            )
            new_ids = [int(r[0]) for r in rows]

        # Keep survival curve in sync with new brackets.
        rebuild_game_survival_from_survival_index(engine)

        from io import BytesIO
        import zipfile

        zip_buf = BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for bid in new_ids:
                txt = export_bracket_text(bid)
                z.writestr(f"bracket_{bid}.txt", txt)

        zip_bytes = zip_buf.getvalue()

        if new_ids:
            st.success(
                f"Generated {len(new_ids)} bracket(s). IDs {new_ids[0]}–{new_ids[-1]}"
            )
        else:
            st.success("Generated 0 bracket(s).")
        st.download_button(
            label="Download ZIP",
            data=zip_bytes,
            file_name=f"brackets_{new_ids[0]}_{new_ids[-1]}.zip",
            mime="application/zip",
        )

with col_right:
    st.subheader("View / download a bracket")

    # Recent list to choose from
    recent = get_latest_brackets(limit=20)
    options = [b.id for b in recent]
    default_id = st.session_state.get("last_bracket_id") if "last_bracket_id" in st.session_state else (options[0] if options else None)

    bracket_id = st.number_input(
        "Bracket ID",
        min_value=1,
        value=int(default_id) if default_id else 1,
        step=1,
    )

    if st.button("Load bracket"):
        if not bracket_exists(bracket_id):
            st.session_state.pop("loaded_bracket_text", None)
            st.session_state.pop("loaded_bracket_id", None)
            st.error(f"Bracket {bracket_id} does not exist.")
        else:
            text_out = export_bracket_text(bracket_id)
            st.session_state["loaded_bracket_text"] = text_out
            st.session_state["loaded_bracket_id"] = int(bracket_id)

    loaded_text = st.session_state.get("loaded_bracket_text")
    loaded_id = st.session_state.get("loaded_bracket_id")
    if loaded_text and loaded_id:
        st.text_area("Bracket", loaded_text, height=600)

        st.download_button(
            label="Download as .txt",
            data=loaded_text,
            file_name=f"bracket_{loaded_id}.txt",
            mime="text/plain",
        )