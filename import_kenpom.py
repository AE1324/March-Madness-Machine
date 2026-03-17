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
                team.adj_em = to_float(row.get("adj_em", "")) or 0.0
                team.adj_o = to_float(row.get("adj_o", "")) or 0.0
                team.adj_d = to_float(row.get("adj_d", "")) or 0.0
                team.adj_tempo = to_float(row.get("adj_tempo", "")) or 0.0
                team.luck = to_float(row.get("luck", "")) or 0.0
                team.sos_adj_em = to_float(row.get("sos_adj_em", "")) or 0.0
                team.sos_adj_o = to_float(row.get("sos_adj_o", "")) or 0.0
                team.sos_adj_d = to_float(row.get("sos_adj_d", "")) or 0.0
                team.ncsos_adj_em = to_float(row.get("ncsos_adj_em", "")) or 0.0

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