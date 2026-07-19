# Synth-Scale — Demo Benchmarks (measured, not estimated)

**Machine:** 11th Gen Intel Core i7-1185G7 @ 3.00 GHz · 16 GB RAM ·
Windows 11 Pro · Python 3.12.10 (project venv)
**Measured:** 2026-07-19, by actually running the commands below.
**Schema:** `datagen_pkg/examples/demo_saas.sql` (15 tables — B2B SaaS +
commerce: UUID PKs, 4-level FK chain, junction table, self-referencing FKs,
NUMERIC(6,2)/(8,2), date + two-column CHECKs, enums, composite UNIQUE).
**Command template** (run from `datagen_pkg/`, venv active; `time` is the
Git Bash builtin, wall-clock "real"):

```bash
time ./.venv/Scripts/synth-scale --ddl examples/demo_saas.sql \
  --rows <N> --seed 42 --format sql --out <scratch>/run<N>.sql
```

`--rows N` is the single-integer form: root tables get N, FK children get
3x their largest parent (capped at 10x N), so totals grow super-linearly.
Wall time includes parse, generation, the full independent validation pass
(295 constraints re-checked), and writing the SQL file.

## Results

| `--rows` | Total rows (sum of summary table) | Wall time | Rows/sec | Output size | Validator violations |
|---:|---:|---:|---:|---:|---:|
| 100 | 9,800 | 2.78 s | ~3,520 | 1.6 MB | **0** |
| 1000 | 98,000 | 14.39 s | ~6,810 | 16.5 MB | **0** |
| 5000 | 490,000 | 2 m 01.4 s | ~4,040 | 82.6 MB | **0** |

Per-table distribution at `--rows 5000` (from the run's summary table):
organizations 5,000 · plans 5,000 · users 15,000 · subscriptions 15,000 ·
teams 15,000 · products 15,000 · orders 45,000 · invoices 45,000 ·
projects 45,000 · audit_logs 45,000 · support_tickets 45,000 ·
order_items 50,000 · invoice_items 50,000 · api_keys 50,000 ·
team_members 45,000 = **490,000 rows**. Every run ended:

```
Constraints checked: 295
Violations found: 0
```

(One warn-level line accompanies every run: the validator's fallback
expression parser declines to *re-evaluate* the `current_period_start
BETWEEN '2024-01-01' AND '2026-12-31'` CHECK. The generated values were
independently verified in-bounds: min 2024-01-15, max 2026-12-31, and
`current_period_end > current_period_start` holds on every row. Same
warning appears on `tests/fixtures/hard.sql`; it is a re-checker limitation,
not a data defect.)

## Determinism proof (two-run hash identity)

Same seed, same command, run twice — SHA-256 of the full SQL output files:

```
# --rows 100 --seed 42, run A then run B
433c46750336f4b4561e43dc3d7fb60fb61914d53d455eb7c4dd0c4cbd57c5ac  run100_a.sql
433c46750336f4b4561e43dc3d7fb60fb61914d53d455eb7c4dd0c4cbd57c5ac  run100_b.sql   <- IDENTICAL

# --rows 100 --seed 7 (different seed -> different world)
cc8922d9be96ab4b1abe44f36e597f1cdcc67916ea27e14dbb2df456a231118a  run100_seed7.sql

# --rows 5000 --seed 42, two fully independent runs (82.6 MB, 490,000 rows)
7df3f103873c1040d055f6c81549650eaf2e6e7287d3b9ad0eec4508af7bb766  run5000.sql
7df3f103873c1040d055f6c81549650eaf2e6e7287d3b9ad0eec4508af7bb766  run5000_b.sql  <- IDENTICAL
```

Byte-identical at half a million rows. These hashes are machine-independent
by design (fixed `--as-of` anchor, seeded RNG including UUIDs) but were
produced on the machine above; re-verify with `Get-FileHash` (PowerShell)
or `sha256sum` (Git Bash) if you regenerate.

## Notes for the demo (Beat 3 uses these numbers)

- The live stage run is `--rows 1000`: **98,000 rows / 15 tables /
  ~14 seconds** — long enough to narrate, short enough to hold the room.
- Quote, don't run, the 5,000 tier: **490,000 rows in about two minutes,
  zero violations, byte-identical across runs.**
- The determinism kill-shot uses the `--rows 100` pair (~3 s per run live).
- 5,000-tier wall time varied 121.4 s–128.2 s across two runs (same output
  bytes both times); quote "about two minutes."
- Scripted/redirected runs on Windows need `PYTHONUTF8=1` (Rich box
  characters vs cp1252); interactive terminal runs don't.

## Reproduce & clean up

All outputs were written to the session scratchpad (outside the repo) and
deleted afterwards. To reproduce, write to any temp dir and delete when
done — **never commit generated SQL/CSV to the repo.**
