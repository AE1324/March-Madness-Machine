## March Madness Bracket Simulator

This project simulates March Madness brackets using a **KenPom‑driven, historically‑calibrated probability model**, and stores bracket outcomes in PostgreSQL in a **compact bit‑packed format** so you can:

- track how long brackets stay perfect,
- see pick distributions by round/team,
- monitor a live leaderboard as real results are entered,
- and export any bracket (or batches) as text files.

The primary UI is a **Streamlit web app** (`app.py`).

---

## 1. Prerequisites

- Python 3.10+
- `pip`
- PostgreSQL (typically via Docker)

Set the database URL in every terminal session that runs the app or CLI:

```bash
export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/brackets"
```

---

## 2. Install dependencies

From the repo root:

```bash
pip install -r requirements.txt
```

Key libraries:

- `sqlalchemy` – database ORM
- `psycopg2-binary` – Postgres driver
- `streamlit` – web UI
- `tqdm` – progress bars for batch generation

---

## 3. Project structure

- `db.py` – database engine + `init_db()` (uses `DATABASE_URL` if set, else SQLite as a fallback).
- `models.py` – SQLAlchemy models:
  - `Team` (seed, region, KenPom fields like `adj_em`, `kenpom_rank`, etc.)
  - `TournamentGame` (bracket structure, round/region/slot)
  - `RealResult` (actual tournament outcomes)
  - `Bracket` (one row per simulated bracket)
  - `BracketPick` exists for backward compatibility, but new simulations store all 63 game outcomes in `brackets.result_bits` (bit-packed). This avoids catastrophic scaling from “one row per pick”.
- `load_bracket.py` – loads an official bracket JSON (`MM_2026.json`) into `teams` and `tournament_games`.
- `import_kenpom.py` – imports cleaned KenPom metrics into `Team` rows (AdjEM, tempo, luck, SOS, rank).
- `simulate.py` – core simulation logic and `win_probability` model:
  - blends KenPom AdjEM with historical seed‑vs‑seed win rates by round,
  - adds game‑level performance variance and region‑level chaos,
  - caps extreme upsets to realistic modern rates.
- `view_bracket.py` – renders a single bracket as readable text (or writes it to a `.txt` file).
- `stats.py` – helper queries for:
  - perfect brackets remaining,
  - leaderboard (correct picks so far),
  - pick percentages by round/team.
- `main.py` – CLI entry point for DB init, bracket loading, and batch generation.
- `app.py` – Streamlit app that wraps everything in a browser‑based UI.

Optional helper(s) you may have:

- `clean_kenpom_csv.py` – turns raw KenPom export into a clean CSV.

---

## 4. One‑time setup

### 4.1 Initialize tables

```bash
python main.py --init-db
```

### 4.2 Load the 2026 bracket structure

The main bracket file is `MM_2026.json` (64‑team field wired through all 6 rounds).

```bash
python main.py --load-bracket MM_2026.json
```

### 4.3 Import KenPom ratings

First, ensure you have a cleaned KenPom CSV (e.g. `kenpom_2026_clean.csv`) with at least:

- `team_name`
- `adj_em`, `adj_o`, `adj_d`, `adj_tempo`
- `luck`
- `sos_adj_em`, `sos_adj_o`, `sos_adj_d`
- `ncsos_adj_em`

Rows should be sorted by KenPom rank (best to worst). Then:

```bash
python import_kenpom.py kenpom_2026_clean.csv
```

This:

- matches `team_name` to `teams.name`,
- sets `Team.kenpom_rank` based on row order,
- fills all KenPom metrics,
- and sets `Team.rating = Team.adj_em` for compatibility.

You can verify:

```bash
docker exec -it mm-postgres psql -U postgres -d brackets -c "
select count(*) total,
       count(adj_em) with_adj_em,
       count(kenpom_rank) with_rank
from teams;"
```

You want `with_adj_em = 64` and `with_rank = 64`.

---

## 5. Running the web app

Start Streamlit:

```bash
export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/brackets"
streamlit run app.py
```

Tabs in the app:

- **Generate**
  - Generate N brackets with the current probability model.
  - “Generate & download ZIP” button to get a `.zip` of `bracket_<id>.txt` files.
- **View/Download**
  - View a specific bracket by ID in the browser.
  - Download that bracket as a `.txt` file.
- **Enter Results**
  - Pick a game (by round/slot) and click which team actually won.
  - Results are stored in `real_results` and used for scoring.
- **Stats**
  - **Perfect brackets remaining** (no wrong picks vs all entered results).
  - **Leaderboard**: brackets sorted by correct picks so far.
  - **Pick % by round**: how often each team is picked to advance in a given round.
- **Admin**
  - Button to truncate all generated brackets/picks and reset IDs (keeps teams/games/results).
  - “Recompute pick percentages and brackets at risk” scans generated brackets and shows progress while it runs.

---

## 6. How the probability model works (high‑level)

For each game:

- **KenPom strength**:
  - Use AdjEM (and KenPom ranks) for `Team.adj_em` and `Team.kenpom_rank`.
  - Apply a round‑dependent logistic with non‑linear gap compression.
- **Historical priors**:
  - Seed‑vs‑seed win rates by round are used as a Bayesian prior (in logit space), with:
    - stronger influence in early rounds (R64),
    - decaying influence by Final Four / Championship.
- **Game‑level variance**:
  - Each team gets a per‑game performance shock (Gaussian in “points”) added to AdjEM.
  - This models hot/cold shooting, fouls, fatigue, etc.
- **Region‑level chaos**:
  - Each (round, region) gets a shared random shift in logit space, creating upset clusters and path correlations.
- **Extreme upset caps**:
  - For R64 only, max underdog win probabilities for 1–16, 2–15, 3–14, 4–13 are capped to realistic modern rates.

Together, this produces:

- realistic R64 upset counts and distributions,
- plausible Final Four/champion distributions,
- and high bracket entropy suitable for “perfect bracket survival” experiments.

---

## 7. Command‑line usage (without the app)

### 7.1 Generate brackets

```bash
python main.py --generate 1000
```

Each bracket:

- gets a unique ID in `brackets.id`,
- stores all 63 game outcomes in `brackets.result_bits` (packed bits).
- `bracket_picks` may remain empty for new simulations (legacy/backward compatibility only).

### 7.2 View/export a single bracket

```bash
python view_bracket.py 42
python view_bracket.py 42 --out bracket_42.txt
```

---

## 8. Resetting generated brackets

If you want to discard all generated brackets but keep the tournament structure and results:

### From the CLI

```bash
docker exec -i mm-postgres psql -U postgres -d brackets <<'SQL'
TRUNCATE TABLE
  brackets
RESTART IDENTITY CASCADE;
SQL
```

### From the app

Use the **Admin** tab → “Delete ALL brackets and picks”.

---

## 9. Scaling notes

- 1,000,000 brackets → 1,000,000 `brackets` rows (bit-packed `result_bits`), plus derived/aggregate tables used by the UI.
- Use batch generation (e.g. 10k at a time) and avoid heavy full‑table scans in the app.
- The stats helpers are written to aggregate efficiently, but for extreme scales you may want to:
  - pre‑aggregate pick stats into a summary table,
  - and cache per‑bracket scores after each real‑life round.


