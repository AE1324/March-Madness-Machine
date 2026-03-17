import csv
import argparse
import re
from pathlib import Path

WL_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")

TARGET_FIELDS = [
    "team_name",
    "conference",
    "w",
    "l",
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


def load_name_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    m: dict[str, str] = {}
    with p.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            k = (row.get("kenpom_name") or "").strip()
            v = (row.get("bracket_name") or "").strip()
            if k and v:
                m[k] = v
    return m


def parse_wl(s: str) -> tuple[str, str]:
    m = WL_RE.match(s or "")
    if not m:
        raise ValueError(f"Bad W-L value: {s!r}")
    return m.group(1), m.group(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean KenPom CSV export into a model-friendly CSV.")
    ap.add_argument("in_csv", help="Your KenPom CSV export")
    ap.add_argument("out_csv", help="Output cleaned CSV")
    ap.add_argument("--name-map", default=None, help="Optional CSV with columns: kenpom_name,bracket_name")
    args = ap.parse_args()

    name_map = load_name_map(args.name_map)

    with open(args.in_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Find the real header row (starts with Rk,Team,Conference,...)
    header_idx = None
    for i, r in enumerate(rows):
        if len(r) >= 4 and (r[0] or "").strip() == "Rk" and (r[1] or "").strip() == "Team":
            header_idx = i
            break
    if header_idx is None:
        raise SystemExit("Could not find the KenPom header row starting with: Rk,Team,Conference,...")

    data_rows = rows[header_idx + 1 :]

    with open(args.out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TARGET_FIELDS)
        w.writeheader()

        for r in data_rows:
            # Skip empty lines
            if not r or not any((c or "").strip() for c in r):
                continue

            # Pad to at least 13 columns (ignore any extras)
            r = (r + [""] * 30)

            team_raw = (r[1] or "").strip()
            conf = (r[2] or "").strip()
            wl = (r[3] or "").strip()

            if not team_raw or not conf or not wl:
                continue

            wins, losses = parse_wl(wl)

            team_name = name_map.get(team_raw, team_raw)

            w.writerow(
                {
                    "team_name": team_name,
                    "conference": conf,
                    "w": wins,
                    "l": losses,
                    "adj_em": (r[4] or "").strip(),
                    "adj_o": (r[5] or "").strip(),
                    "adj_d": (r[6] or "").strip(),
                    "adj_tempo": (r[7] or "").strip(),
                    "luck": (r[8] or "").strip(),
                    "sos_adj_em": (r[9] or "").strip(),
                    "sos_adj_o": (r[10] or "").strip(),
                    "sos_adj_d": (r[11] or "").strip(),
                    "ncsos_adj_em": (r[12] or "").strip(),
                }
            )

    print(f"Wrote {args.out_csv}")


if __name__ == "__main__":
    main()