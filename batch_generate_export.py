import os
import argparse
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from simulate import simulate_single_bracket


def main() -> None:
    p = argparse.ArgumentParser(description="Generate N brackets and export to .txt + zip.")
    p.add_argument("n", type=int, help="How many brackets to generate")
    p.add_argument("--outdir", default="exported_brackets", help="Output folder for bracket txt files")
    p.add_argument("--zip", default="exported_brackets.zip", help="Zip filename to create")
    args = p.parse_args()

    db_url = os.getenv("DATABASE_URL", "sqlite:///brackets.db")
    engine = create_engine(db_url, future=True)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # 1) Generate N brackets
    new_ids: list[int] = []
    with Session(engine) as session:
        for _ in range(args.n):
            bid = simulate_single_bracket(session)
            new_ids.append(bid)

    # 2) Export each bracket to text
    from view_bracket import main as _  # ensures file exists
    import subprocess
    import sys

    for bid in new_ids:
        outfile = outdir / f"bracket_{bid}.txt"
        subprocess.check_call([sys.executable, "view_bracket.py", str(bid), "--out", str(outfile)])

    # 3) Zip the folder
    zip_path = Path(args.zip)
    if zip_path.exists():
        zip_path.unlink()

    # Use system zip if available
    subprocess.check_call(["zip", "-r", str(zip_path), str(outdir)])

    print(f"Generated {len(new_ids)} bracket(s).")
    print(f"Wrote folder: {outdir}")
    print(f"Wrote zip: {zip_path}")


if __name__ == "__main__":
    main()