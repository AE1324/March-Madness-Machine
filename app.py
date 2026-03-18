import os
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


def recompute_pick_stats_and_brackets_at_risk(engine) -> None:
    """
    Recompute `pick_stats` and `brackets_at_risk` using `brackets.result_bits`.
    """
    _ensure_derived_tables_exist(engine)

    with Session(engine) as session:
        teams_by_id = {t.id: t for t in session.query(Team).all()}
        games = (
            session.query(TournamentGame)
            .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
            .all()
        )
        if not games:
            raise RuntimeError("No tournament games found. Load a bracket first.")

        played_rows = session.query(RealResult.game_id, RealResult.winner_team_id).all()
        played_winner_by_game_id = {
            gid: wid for gid, wid in played_rows if wid is not None
        }
        result_game_ids = {gid for gid, _ in played_rows}
        future_games = [g for g in games if g.id not in result_game_ids]

        bracket_q = session.query(Bracket.id, Bracket.result_bits).filter(
            Bracket.result_bits.isnot(None)
        )
        bracket_count = bracket_q.count()
        if bracket_count == 0:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM pick_stats;"))
                conn.execute(text("DELETE FROM brackets_at_risk;"))
            return

        pick_counts: dict[tuple[int, int], int] = {}
        risk_counts: dict[tuple[int, str | None, str, int], int] = {}
        perfect_brackets = 0

        for _, bits in bracket_q.yield_per(2000):
            assert bits is not None
            winners_by_game_id = decode_bracket_winners(
                int(bits), games, teams_by_id
            )

            # Pick distributions by round (all brackets).
            for g in games:
                win_tid = winners_by_game_id[g.id]
                key = (g.round, win_tid)
                pick_counts[key] = pick_counts.get(key, 0) + 1

            # Perfect bracket check (played games only).
            is_perfect = True
            for gid, actual_winner_tid in played_winner_by_game_id.items():
                if winners_by_game_id.get(gid) != actual_winner_tid:
                    is_perfect = False
                    break

            if is_perfect:
                perfect_brackets += 1
                for g in future_games:
                    win_tid = winners_by_game_id[g.id]
                    rkey = (g.round, g.region, g.slot, win_tid)
                    risk_counts[rkey] = risk_counts.get(rkey, 0) + 1

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


# --- Streamlit UI ---

st.set_page_config(page_title="Bracket Simulator", layout="wide")

try:
    ensure_bracket_loaded("MM_2026.json")
    _ensure_derived_tables_exist(get_engine())
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
            conn.execute(text("TRUNCATE TABLE bracket_picks, brackets RESTART IDENTITY CASCADE;"))
        st.success("All brackets and picks deleted.")

    clear_results = st.checkbox("Confirm: delete ALL entered real results")
    if st.button("Delete real results", disabled=not clear_results):
        engine = get_engine()
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE real_results RESTART IDENTITY CASCADE;"))
        st.success("All real results deleted.")


    
    st.subheader("Pick stats")
    
    if st.button("Recompute pick percentages and brackets at risk (all brackets)"):
        engine = get_engine()
        with st.spinner("Recomputing pick stats from packed bracket outcomes..."):
            recompute_pick_stats_and_brackets_at_risk(engine)
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
            st.error(f"Bracket {bracket_id} does not exist.")
        else:
            text_out = export_bracket_text(bracket_id)
            st.text_area("Bracket", text_out, height=600)

            # Download button
            st.download_button(
                label="Download as .txt",
                data=text_out,
                file_name=f"bracket_{bracket_id}.txt",
                mime="text/plain",
            )