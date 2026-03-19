## Architecture

```mermaid
flowchart TD
  User[User] --> UI[Streamlit app: app.py]
  UI -->|SQLAlchemy ORM| DB[(PostgreSQL)]

  subgraph Data[Core tables]
    Teams[teams]
    Games[tournament_games]
    Real[real_results]
    Brackets[brackets (bit-packed result_bits)]
    Survival[game_survival (derived)]
    PickStats[pick_stats (derived)]
    AtRisk[brackets_at_risk (derived)]
  end

  UI -->|load JSON| Loader[load_bracket.py]
  UI -->|import KenPom CSV| KenPom[import_kenpom.py]
  UI -->|simulate & bulk load| Sim[simulate.py]

  Sim --> Brackets
  UI -->|enter real outcomes| Real
  UI -->|incremental SQL update| Survival
  UI -->|scan+aggregate| PickStats
  UI -->|scan+aggregate| AtRisk

  UI --> View[view_bracket.py: decode & export]
```

### Key design choices
1. **Bit-packed bracket outcomes**: each bracket stores 63 game outcomes in a single `BIGINT` (`brackets.result_bits`), avoiding a “one row per pick” scaling blow-up.
2. **Survival tracking**: `brackets.survival_index` supports fast “perfect bracket remaining” without decoding every bracket after each real game.
3. **Derived analytics tables**: the UI reads precomputed aggregates (`game_survival`, `pick_stats`, `brackets_at_risk`) rather than recomputing dashboards on every page load.

