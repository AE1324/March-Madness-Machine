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

Example: see `.env.example`.

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
  - blends KenPom AdjEM (plus per-game shocks) with a seed-structural prior in logit space,
  - adds region-level correlated noise and round-dependent temperature scaling.
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

The simulator computes a per-game win probability in `simulate.py`:
- **Strength + shocks**: AdjEM is perturbed by round-dependent Gaussian “performance shocks”.
- **Optional rank correction**: KenPom rank difference nudges the win probability when available.
- **Round-dependent gap compression**: the strength gap is transformed with a round-specific exponent.
- **Seed structural prior**: seed distance adds a structured prior, blended with the KenPom term in logit space.
- **Region systemic noise**: each (round, region) shares a correlated noise component to generate realistic upset paths.
- **Temperature scaling**: a round-specific temperature sharpens/softens probabilities.

The generation path encodes all 63 game outcomes into `brackets.result_bits` for efficient storage.

---

## 7. Command‑line usage (without the app)

### 7.1 Generate brackets

```bash
python main.py --generate 1000
```

For reproducibility, you can also pass a fixed RNG seed:
`python main.py --generate 1000 --model-version v1 --seed 12345`

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
  bracket_picks,
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


---

## Research-Grade Repo Extras

### Overview
This repository provides a KenPom-driven simulator that generates large volumes of March Madness brackets and stores outcomes in PostgreSQL using a compact bit-packed representation. The Streamlit dashboard supports:
- perfect bracket survival tracking
- pick distribution analytics
- a live leaderboard as you enter real game results

### Architecture Diagram
See: `docs/ARCHITECTURE.md`

```mermaid
flowchart TD
  User[User] --> UI[Streamlit app: app.py]
  UI -->|SQLAlchemy ORM| DB[(PostgreSQL)]
  UI -->|simulate & bulk load| Sim[simulate.py]
  Sim --> Brackets[brackets.result_bits (bit-packed)]
  UI -->|enter real outcomes| Real[real_results]
  UI -->|incremental SQL update| Survival[brackets.survival_index → game_survival]
  UI -->|scan+aggregate| Stats[pick_stats / brackets_at_risk]
  UI --> View[view_bracket.py (decode & export)]
```

### Model Methodology
See: `docs/MODEL_METHODODOLOGY.md`

### Reproducible Instructions
See: `docs/REPRODUCIBILITY.md`

Key point: bracket generation supports an optional RNG seed for deterministic single-process runs:
`python main.py --generate N --model-version v1 --seed 12345`

### Running Simulations Locally
1. Start PostgreSQL and set `DATABASE_URL`:
   ```bash
   export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/brackets"
   ```
2. Initialize + load tournament structure + import KenPom:
   ```bash
   python main.py --init-db
   python main.py --load-bracket MM_2026.json
   python import_kenpom.py /path/to/kenpom_2026_clean.csv
   ```
3. Generate brackets:
   ```bash
   python main.py --generate 1000000 --model-version v1 --seed 12345
   ```
4. Start the dashboard:
   ```bash
   streamlit run app.py
   ```

### Generating Millions of Brackets
- Use large values of `N` with a fixed `model_version`.
- For dashboard operations, expect “Admin recompute” to be the slowest step because it decodes and aggregates across stored brackets.
- If you plan to store tens/hundreds of millions, ensure Docker Desktop disk image size is large enough for Postgres + WAL headroom (see `docs/BENCHMARKS.md` and Docker settings).

### Database Scaling Strategy
Main strategy:
- store 63 outcomes per bracket in `brackets.result_bits` (single `BIGINT`)
- track “perfect bracket remaining” using `brackets.survival_index` (no decode needed after each real game)
- keep dashboard responsive via derived tables (`game_survival`, `pick_stats`, `brackets_at_risk`)

### Visualization Dashboard
See: `docs/DASHBOARD_GUIDE.md`

### Example Outputs
This repo includes example tournament JSON graphs:
- `bracket_example.json`
- `bracket_example_no_ratings.json`

To export a generated bracket to text:
```bash
python view_bracket.py <BRACKET_ID> --out bracket_<BRACKET_ID>.txt
```

### Performance Benchmarks
See: `docs/BENCHMARKS.md`

### Dashboard Screenshots
See: `docs/SCREENSHOTS.md`


