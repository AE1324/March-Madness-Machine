import os
import csv
import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Team

KENPOM_FIELDS = [
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


def to_float(v: str):
    v = (v or "").strip()
    if v == "":
        return None
    return float(v)


def main() -> None:
    p = argparse.ArgumentParser(description="Import KenPom metrics CSV into Team columns")
    p.add_argument("csv_path", help="kenpom_2026_clean.csv")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db_url = os.getenv("DATABASE_URL", "sqlite:///brackets.db")
    engine = create_engine(db_url, future=True)

    with Session(engine) as session:
        teams = session.query(Team).all()
        teams_by_name = {t.name: t for t in teams}

        updated = 0
        missing: list[str] = []

        with open(args.csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = (row.get("team_name") or "").strip()
                if not name:
                    continue

                team = teams_by_name.get(name)
                if not team:
                    missing.append(name)
                    continue

                # Fill explicit KenPom columns on Team
                rk_str = (row.get("rk") or "").strip()
                rank = int(rk_str) if rk_str else None

                team.kenpom_rank = rank
                team.adj_em = float(row["adj_em"])
                team.adj_o = float(row["adj_o"])
                team.adj_d = float(row["adj_d"])
                team.adj_tempo = float(row["adj_tempo"])
                team.luck = float(row["luck"])
                team.sos_adj_em = float(row["sos_adj_em"])
                team.sos_adj_o = float(row["sos_adj_o"])
                team.sos_adj_d = float(row["sos_adj_d"])
                team.ncsos_adj_em = float(row["ncsos_adj_em"])

                # keep rating = AdjEM for compatibility
                team.rating = team.adj_em

                # Also keep teams.rating in sync with AdjEM (backward compatibility)
                team.rating = team.adj_em

                updated += 1

        if missing:
            print("\nNames in KenPom CSV not found in DB (must match teams.name exactly):")
            for n in sorted(set(missing))[:60]:
                print(f"- {n}")
            extra = len(set(missing)) - 60
            if extra > 0:
                print(f"... and {extra} more")

        if args.dry_run:
            session.rollback()
            print(f"\nDry run complete. Would update {updated} teams.")
            return

        session.commit()
        print(f"\nImported KenPom metrics for {updated} teams.")


if __name__ == "__main__":
    main()