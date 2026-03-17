import os
import argparse
import re
from collections import defaultdict

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Team, TournamentGame, BracketPick

REGIONS = ["East", "West", "South", "Midwest"]
ROUND_LABEL = {1: "Round of 64", 2: "Round of 32", 3: "Sweet 16", 4: "Elite 8"}

# Expected slot patterns like: EAST_R64_G1, WEST_R32_G3, SOUTH_S16_G2, MIDWEST_E8_G1
SLOT_RE = re.compile(r"^(EAST|WEST|SOUTH|MIDWEST)_(R64|R32|S16|E8)_G(\d+)$", re.IGNORECASE)


def _slot_gnum(slot: str) -> int:
    m = SLOT_RE.match(slot or "")
    return int(m.group(3)) if m else 10_000


def _resolve_team_id(source: str, winner_by_game_id: dict[int, int]) -> int:
    if source.startswith("TEAM-"):
        return int(source.split("-", 1)[1])
    if source.startswith("WIN-"):
        game_id = int(source.split("-", 1)[1])
        return winner_by_game_id[game_id]
    raise ValueError(f"Unknown team source format: {source}")


def _seeded_name(team: Team) -> str:
    # Always show seed, all rounds
    return f"({team.seed}) {team.name}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a bracket in a classic template-like format.")
    parser.add_argument("bracket_id", type=int, help="Bracket ID to export")
    parser.add_argument("--out", help="Write output to this file instead of printing")
    args = parser.parse_args()

    out_lines: list[str] = []

    def emit(s: str = "") -> None:
        out_lines.append(s)

    db_url = os.getenv("DATABASE_URL", "sqlite:///brackets.db")
    engine = create_engine(db_url, future=True)

    with Session(engine) as session:
        teams = session.query(Team).all()
        teams_by_id = {t.id: t for t in teams}

        picks = (
            session.query(BracketPick)
            .filter(BracketPick.bracket_id == args.bracket_id)
            .all()
        )
        if not picks:
            raise SystemExit(f"No picks found for bracket_id={args.bracket_id}")

        winner_by_game_id = {p.game_id: p.predicted_winner_team_id for p in picks}

        games = (
            session.query(TournamentGame)
            .order_by(TournamentGame.round.asc(), TournamentGame.id.asc())
            .all()
        )
        if not games:
            raise SystemExit("No tournament games found. Load a bracket first.")

        # Build per-region per-round ordered lists of games
        region_games: dict[str, dict[int, list[TournamentGame]]] = {r: defaultdict(list) for r in REGIONS}
        final_four_games: list[TournamentGame] = []
        championship_games: list[TournamentGame] = []

        for g in games:
            if g.region in REGIONS and 1 <= g.round <= 4:
                region_games[g.region][g.round].append(g)
            elif g.round == 5:
                final_four_games.append(g)
            elif g.round == 6:
                championship_games.append(g)

        # sort each round in each region by G#
        for r in REGIONS:
            for rnd in (1, 2, 3, 4):
                region_games[r][rnd].sort(key=lambda gg: _slot_gnum(gg.slot))

        def seeded_from_source(source: str) -> str:
            tid = _resolve_team_id(source, winner_by_game_id)
            return _seeded_name(teams_by_id[tid])

        emit("MARCH MADNESS BRACKET (64 TEAMS)")
        emit("")
        emit(f"BRACKET ID: {args.bracket_id}")
        emit("")

        # Regions in the exact order the user showed
        for region in ["East", "West", "South", "Midwest"]:
            emit(f"{region.upper()} REGION")

            # Round of 64
            emit(ROUND_LABEL[1])
            r64 = region_games[region][1]
            for g in r64:
                emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
            emit("")

            # Round of 32
            emit(ROUND_LABEL[2])
            r32 = region_games[region][2]
            for g in r32:
                emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
            emit("")

            # Sweet 16
            emit(ROUND_LABEL[3])
            s16 = region_games[region][3]
            for g in s16:
                emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
            emit("")

            # Elite 8
            emit(ROUND_LABEL[4])
            e8 = region_games[region][4]
            if e8:
                g = e8[0]
                emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
            else:
                emit("__________ vs __________")
            emit("")
            emit("")

        # Final Four
        emit("FINAL FOUR")
        final_four_games.sort(key=lambda g: g.slot)

        if len(final_four_games) >= 1:
            g = final_four_games[0]
            emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
        else:
            emit("__________ vs __________")

        if len(final_four_games) >= 2:
            g = final_four_games[1]
            emit(f"{seeded_from_source(g.team1_source)} vs {seeded_from_source(g.team2_source)}")
        else:
            emit("__________ vs __________")

        emit("")
        emit("NATIONAL CHAMPIONSHIP")

        championship_games.sort(key=lambda g: g.slot)
        champ = championship_games[0] if championship_games else None

        if champ:
            emit(f"{seeded_from_source(champ.team1_source)} vs {seeded_from_source(champ.team2_source)}")
            champ_team_id = winner_by_game_id[champ.id]
            champion_name = _seeded_name(teams_by_id[champ_team_id])
        else:
            emit("__________ vs __________")
            champion_name = "__________"

        emit("")
        emit(f"NATIONAL CHAMPION {champion_name}")
        emit("")

    output_text = "\n".join(out_lines)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output_text + "\n")
        print(f"Wrote {args.out}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()