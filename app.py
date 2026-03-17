import os
from io import StringIO

import zipfile
from io import BytesIO

import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from db import engine as default_engine
from models import Team, TournamentGame, Bracket, BracketPick
from simulate import simulate_single_bracket
from view_bracket import main as view_bracket_main  # we'll re-use its logic indirectly
from main import ensure_bracket_loaded

from sqlalchemy import text
from stats import count_perfect_brackets, leaderboard, pick_percentages_by_round


# --- DB helpers ---

def get_engine():
    url = os.getenv("DATABASE_URL")
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
        teams = session.query(Team).all()
        teams_by_id = {t.id: t for t in teams}

        picks = (
            session.query(BracketPick)
            .filter(BracketPick.bracket_id == bracket_id)
            .all()
        )
        if not picks:
            return f"No picks found for bracket {bracket_id}\n"

        winner_by_game_id = {p.game_id: p.predicted_winner_team_id for p in picks}
        games = (
            session.query(TournamentGame)
            .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
            .all()
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


# --- Streamlit UI ---

st.set_page_config(page_title="Bracket Simulator", layout="wide")

ensure_bracket_loaded("MM_2026.json")

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
        st.dataframe(pick_percentages_by_round(session, rnd), use_container_width=True)


with tab_admin:
    st.subheader("Admin")

    st.write("Clear all generated brackets (keeps teams, games, and results).")

    if st.button("Delete ALL brackets and picks"):
        engine = get_engine()
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE bracket_picks, brackets RESTART IDENTITY CASCADE;"))
        st.success("All brackets and picks deleted. You can generate fresh ones now.")

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
        new_ids = []
        with Session(engine) as session:
            from simulate import simulate_single_bracket as sim_one
            for _ in range(int(n)):
                bid = sim_one(session)
                new_ids.append(bid)
        if new_ids:
            st.success(f"Generated {len(new_ids)} bracket(s). Latest ID: {new_ids[-1]}")
            st.session_state["last_bracket_id"] = new_ids[-1]

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
        new_ids = []
        with Session(engine) as session:
            from simulate import simulate_single_bracket as sim_one
            for _ in range(int(zip_n)):
                bid = sim_one(session)
                new_ids.append(bid)

        from io import BytesIO
        import zipfile

        zip_buf = BytesIO()
        with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for bid in new_ids:
                txt = export_bracket_text(bid)
                z.writestr(f"bracket_{bid}.txt", txt)

        zip_bytes = zip_buf.getvalue()

        st.success(f"Generated {len(new_ids)} bracket(s). IDs {new_ids[0]}–{new_ids[-1]}")
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