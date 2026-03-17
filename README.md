# March Madness Bracket Simulator

This project simulates March Madness brackets, generating up to 1,000,000 AI-driven brackets and storing them in a database so you can analyze how long they stay perfect as the real tournament progresses.

The implementation is in Python and uses SQLite by default (no extra database setup required), but can be pointed at PostgreSQL or another database supported by SQLAlchemy.

## 1. Prerequisites

- Python 3.10+ installed (`python --version` to check).
- `pip` installed.

## 2. Install dependencies

From this folder run:

```bash
pip install -r requirements.txt
```

## 3. Project files

- `db.py`: database connection and base model setup.
- `models.py`: SQLAlchemy models for teams, games, brackets, and picks.
- `simulate.py`: functions to simulate brackets and store picks in the database.
- `load_bracket.py`: helper to load an official bracket file (JSON) into the database.
- `main.py`: command-line entry point to generate many brackets.

## 4. Initialize the database

By default the project uses a local SQLite file called `brackets.db` in this folder.

To create the database tables run:

```bash
python main.py --init-db
```

## 5. Load an official bracket

You must provide a bracket description in JSON format. See `bracket_example.json` for the expected structure.

Once you have a JSON file (e.g. `my_bracket_2026.json`) run:

```bash
python main.py --load-bracket my_bracket_2026.json
```

This will populate the `teams` and `tournament_games` tables.

## 6. Generate brackets

To generate 1,000,000 brackets with the default simple rating-based model:

```bash
python main.py --generate 1000000
```

Each generated bracket is stored in the `brackets` table with a unique ID, and all its picks are in the `bracket_picks` table.

You can generate fewer for testing, e.g.:

```bash
python main.py --generate 1000
```

## 7. Tracking real results

As the tournament progresses you can fill in real results and compare them to each bracket's picks.

For now this is done by directly inserting rows into the `real_results` table, for example using a database viewer or a small custom script. Future extensions can automate this.

## 8. Changing database backend

If you want to use PostgreSQL or another database, edit `db.py` and change the `DATABASE_URL`. Any SQLAlchemy-compatible database string will work.

