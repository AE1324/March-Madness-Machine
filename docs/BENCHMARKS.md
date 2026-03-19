## Performance Benchmarks

This project is designed to generate millions of brackets efficiently, but “dashboard” analytics can still become the bottleneck at extreme scale.

### What to measure
Record the following for reproducibility:
1. **Generation throughput**: brackets/second for `N` brackets.
2. **Recompute throughput** (Admin step): brackets/second for
   `Recompute pick percentages and brackets at risk`.
3. Hardware + environment:
   - Mac/VM specs (CPU model, RAM)
   - Docker Desktop disk image size
   - Postgres version
   - dataset size (number of rows in `brackets`)

### How to capture generation timing
Use CLI generation so you can provide RNG seed:

```bash
time python main.py --generate 100000 --model-version v1 --seed 123
```

The `time` output can be copied directly into a benchmark table.

### How to capture dashboard recompute timing
In the Streamlit app:
1. Go to **Admin**
2. Click **“Recompute pick percentages and brackets at risk (all brackets)”**
3. Use the on-screen progress indicator (processed count + elapsed time)
4. Copy the final elapsed time from the status line

The recompute function scans and decodes bracket outcomes, then aggregates into:
- `pick_stats`
- `brackets_at_risk`

### Benchmark template (fill in your numbers)
| Brackets (N) | Generation time | Gen rate | Recompute time | Notes |
|---:|---:|---:|---:|---|
| 100,000 | | | | |
| 1,000,000 | | | | |
| 10,000,000 | | | | |

