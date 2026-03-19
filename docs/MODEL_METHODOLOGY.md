## Model Methodology

### Inputs
For each team in a given year’s bracket structure, the model uses (when available):
1. `Team.adj_em` (KenPom AdjEM) as baseline strength.
2. `Team.seed` as the tournament structural prior (seed-vs-seed “distance”).
3. `Team.kenpom_rank` as a rank-based correction term (only if both sides have a non-negative rank).

For each game (Round/Region slot), the model samples:
1. A **team-level performance shock** (KenPom-point shock).
2. A **region systemic noise** term to induce correlated upset paths.

### Win probability (fast numeric path)
All bracket generation currently uses `simulate_bracket_outcome_bits_fast()` → `win_probability_fast()`.

For a game at `round_num`:
1. **Strength with shock**
   - `ra = strength_base_a + strength_shock_a`
   - `rb = strength_base_b + strength_shock_b`
   - `raw_gap = ra - rb`

2. **KenPom rank correction (optional)**
   - If both `kenpom_rank_a` and `kenpom_rank_b` are present:
     - `raw_gap += 0.018 * (kenpom_rank_b - kenpom_rank_a)`

3. **Non-linear gap compression (round-dependent exponent)**
   - The model compresses the raw gap with an exponent that depends on the round.

4. **Base logistic from gap**
   - `p_model = logistic(gap, SCALE_BY_ROUND[round_num])`

5. **Seed structural prior**
   - Compute seed distance: `seed_diff = abs(seed_a - seed_b)`
   - Convert distance into a seed-proxy win probability curve: `seed_curve = logistic(seed_diff, 2.15)`
   - Apply a round-dependent decay so seed structure influences less later:
     - `p_seed = 0.5 + (seed_curve - 0.5) * decay`
   - Flip if `seed_a` is worse than `seed_b`.

6. **Blend in logit space**
   - Let `alpha = ALPHA_BY_ROUND[round_num]`
   - `z = alpha * logit(p_model) + (1 - alpha) * logit(p_seed)`

7. **Region-level systemic variance**
   - Add correlated noise: `z += region_noise * REGION_CHAOS[round_num]`

8. **Temperature scaling**
   - `p = inv_logit(logit(p) / TEMP_BY_ROUND[round_num])`

The resulting `p` is clamped to `(1e-6, 1 - 1e-6)` for numerical stability.

### Shocks and correlation structure
Bracket generation samples:
1. **Team shocks**: `strength_shock ~ Normal(0, shock_sd_by_round(round_num))`
2. **Region noise**: each (Round, Region) game slot gets a shared noise key; individual games receive the corresponding sampled noise.

Key calibration knobs are implemented in `simulate.py`:
- `shock_sd_by_round(round_num)`
- `REGION_CHAOS[round_num]`

### Bit-packed simulation outputs
For a 64-team bracket, the simulator encodes 63 game outcomes into a single integer:
- `brackets.result_bits` is a 63-bit mask.
- bit `i` corresponds to game bit index `i` (0..62).

This encoding enables:
- fast decoding for per-bracket views,
- fast “perfect survival” queries via `brackets.survival_index`,
- and efficient simulation storage at scale.

