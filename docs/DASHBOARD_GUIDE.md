## Visualization Dashboard

The primary UI is `app.py` (Streamlit).

### Tabs
1. **Generate**
   - Create N brackets using the current model.
   - “Generate & download ZIP” exports generated brackets to `.txt`.
2. **View/Download**
   - Render a bracket by ID using `view_bracket.py` decoding.
3. **Enter Results**
   - Store the official winner for a specific game in `real_results`.
   - Updates `brackets.survival_index` incrementally and rebuilds `game_survival`.
4. **Stats**
   - **Perfect bracket survival**: uses fast derived state (`survival_index` / `game_survival`).
   - **Leaderboard**: scans/decodes bracket winners to count correct picks so far.
   - **Pick percentages by round**: recomputed from decoded bracket winners.
5. **Admin**
   - “Delete ALL brackets and picks”: truncates generated simulation state.
   - “Delete ALL entered real results”: resets `survival_index` and derived survival views.
   - “Recompute pick percentages and brackets at risk (all brackets)”
     - scans and aggregates across all generated brackets
     - now includes an on-screen progress indicator.

### Derived tables
The app uses derived tables to keep the dashboard responsive:
- `game_survival` (survival curve by game index)
- `pick_stats` (pick percentages)
- `brackets_at_risk` (brackets still perfect up to entered games, but requiring a specific future pick)

