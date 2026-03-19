## Reproducibility

To reproduce simulation results you need:
1. The exact `MM_2026.json` bracket structure (teams/games graph).
2. The exact KenPom-derived team ratings imported into `teams`.
3. The simulator configuration: `model_version` and RNG `--seed`.
4. The same simulator code version/commit.

### Deterministic RNG seed
Bracket generation supports an optional RNG seed:
- CLI: `python main.py --generate N --seed SEED`
- Streamlit UI: currently generates without a user-specified seed; use CLI for strict reproducibility.

Seeded runs are deterministic in the current single-process generation path.

### End-to-end reproducible run (example)
Assume your repository root is the project directory.

1) Start PostgreSQL (Docker) and set `DATABASE_URL`:

```bash
export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/brackets"
```

2) Initialize DB schema:

```bash
python main.py --init-db
```

3) Load the bracket structure:

```bash
python main.py --load-bracket MM_2026.json
```

4) Import KenPom:

```bash
python import_kenpom.py /path/to/kenpom_2026_clean.csv
```

5) Generate brackets deterministically:

```bash
python main.py --generate 1000000 --model-version v1 --seed 12345
```

6) Start the dashboard and enter real results:

```bash
streamlit run app.py
```

### Notes on derived tables
The app maintains derived analytics tables such as:
- `brackets.survival_index` (updated incrementally when you enter a real game winner),
- `game_survival` (rebuilt from survival),
- `pick_stats` and `brackets_at_risk` (updated via Admin recomputation).

Given the same DB state, results are reproducible.

