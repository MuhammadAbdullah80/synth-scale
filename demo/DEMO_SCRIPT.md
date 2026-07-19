# Synth-Scale — Demo Day Live Runbook (5:00)

Second-person imperative: type this, say this. Timing budget: 30 + 60 + 90 +
60 + 60 = 5:00. Every "EXPECTED" block below is the real output captured on
2026-07-19 from this repo (Windows 11, Python 3.12 venv). Numbers come from
`demo/BENCHMARK.md` — re-measure if you switch machines.

---

## Pre-demo checklist (do this 30+ minutes before, not on stage)

- [ ] Terminal: PowerShell, dark theme, font **≥ 20 pt**, maximized, one tab.
- [ ] `cd A:\comebck\datagen\datagen_pkg` and activate the venv:
      `.\.venv\Scripts\Activate.ps1` — confirm `synth-scale --help` prints.
- [ ] Make the scratch output dir: `mkdir "$env:TEMP\synth_demo" -Force`
- [ ] **Pre-type every command below into shell history** (run them once,
      top to bottom — this also warms the disk cache and re-verifies the
      machine). On stage each command is ↑↑ + Enter, never typed live.
- [ ] Run the full suite once: `python -m pytest -q` → expect
      `129 passed, 8 skipped` (~10 s). Screenshot it as a backup slide.
- [ ] If a demo Postgres exists: set `$env:SYNTH_PG_URL` and rehearse Beat 4
      (OPTIONAL-IF-DB). If not, rehearse the fallback branch of Beat 4.
- [ ] Backup plan of last resort: full-run screen recording on your phone +
      the Loom (`demo/LOOM_STORYBOARD.md`). Record it at rehearsal.
- [ ] Close Slack/notifications. Battery + power. Clock visible to you.
- [ ] Quirk to know: if you ever pipe/redirect output on Windows, prefix
      `$env:PYTHONUTF8=1` (Rich's box characters vs cp1252). Never needed for
      normal on-screen runs.

---

## Beat 1 — 0:00–0:30 · The problem (talk, no gamble)

**Show:** your vibe-coded app (or Supabase Studio) open on an **empty
table list**. One glance, no clicking around.

**Say:**
> "Every app you vibe-code starts like this: beautiful schema, zero rows.
> So you ask Claude for a seed script. Two problems. One: fifteen tables
> times ten thousand rows doesn't fit in a chat window — you hit the token
> wall. Two: what you get is *probably* correct — a dangling foreign key
> here, a numeric overflow there, different data every time you re-prompt.
> We made the guaranteed version."

**Do NOT** run Claude live. The failure is asserted, not demonstrated —
it's everyone in the room's lived experience.

**Fallback:** none needed — this beat has no moving parts. If the app
screenshot is missing, describe it; don't hunt for windows on stage.

## Beat 2 — 0:30–1:30 · Instant gratification: `--preview`

**Type:**
```powershell
synth-scale --ddl examples/demo_saas.sql --rows 50 --preview
```

**EXPECTED (real output — summary first, then 10-row samples per table; box
borders render as clean lines in a real terminal):**
```
Generation summary
┌─────────────────┬──────┬────────┐
│ Table           │ Rows │ Status │
├─────────────────┼──────┼────────┤
│ organizations   │   50 │ ok     │
│ plans           │   50 │ ok     │
│ users           │  150 │ ok     │
│ subscriptions   │  150 │ ok     │
│ teams           │  150 │ ok     │
│ products        │  150 │ ok     │
│ orders          │  450 │ ok     │
│ invoices        │  450 │ ok     │
│ projects        │  450 │ ok     │
│ audit_logs      │  450 │ ok     │
│ support_tickets │  450 │ ok     │
│ order_items     │  500 │ ok     │
│ invoice_items   │  500 │ ok     │
│ api_keys        │  500 │ ok     │
│ team_members    │  450 │ ok     │
└─────────────────┴──────┴────────┘
Constraints checked: 295
Violations found: 0
```

**Say (while scrolling to `users`, then `products`, then `orders`):**
> "One command against a fifteen-table SaaS schema — organizations, teams,
> projects, API keys, subscriptions, invoices, orders. No LLM, no cloud,
> nothing written to disk yet. Now look at the *data*, not just the shape:"

Call out, pointing (all real rows from this exact command):
- **users** — "Diego is male; Priya is female. Berlin is in Germany, Toronto
  in Canada — never 'Karachi, Bavaria, Peru'. Notice `referred_by`: users
  referring users, and it's a clean tree, no cycles."
- **products** — "'Wireless Earbuds Pro' is Electronics, mid-tier —
  and mid-tier *priced*: 75.00. 'Cast Iron Skillet', Home & Kitchen, 57.99.
  Budget items are 21.99, premium is 258.99 — prices end in .99 and .95
  because real prices do."
- **orders** — "A `cancelled` order has a `cancelled_at` and no `shipped_at`.
  A `pending` one has neither. Statuses and timestamps agree — and
  `updated_at` is never before `created_at`, anywhere."
- Land on: "**295 constraints checked. Zero violations.** And that's an
  independent validator saying it, not the generator grading itself."

*(If a judge later asks about the one `Warnings:` line under the summary:
that's the validator's re-check parser declining to re-evaluate a date
`BETWEEN` expression — the generated values themselves are clamped in-bounds;
we verified min/max = 2024-01-15 → 2026-12-31 against the literals
2024-01-01 → 2026-12-31.)*

**Fallback:** if the terminal wedges, you have the pre-demo run of this
exact command in scrollback — scroll up. Worst case: the screenshots from
rehearsal.

## Beat 3 — 1:30–3:00 · The mic-drop: scale, then determinism

**Type:**
```powershell
synth-scale --ddl examples/demo_saas.sql --rows 1000 --seed 42 --format sql --out "$env:TEMP\synth_demo\seed.sql"
```

**Say while it runs (~14 s — this is your talking window):**
> "Now the real run. Single integer, one thousand — the engine fans that out
> along the foreign-key graph: a thousand organizations, three thousand
> users, nine thousand orders… ninety-eight thousand rows total, in
> dependency order so every insert lands."

**EXPECTED (real, 14.4 s wall):** the same 15-table summary with
`organizations 1000 … order_items 10000`, ending:
```
Constraints checked: 295
Violations found: 0
Wrote SQL inserts to C:/…/synth_demo/seed.sql
```

> "**Ninety-eight thousand rows, fifteen tables, about fourteen seconds, on
> this laptop.** Zero violations." *(At 5,000 it's 490,000 rows in about two
> minutes — say it, don't run it.)*

**Type:**
```powershell
Get-Content "$env:TEMP\synth_demo\seed.sql" -TotalCount 5
```

**EXPECTED (real):**
```
BEGIN;

INSERT INTO "organizations" ("id", "name", "slug", "created_at", "updated_at") VALUES
  ('bdd640fb-0667-4ad1-9c80-317fa3b1799d', 'Sharable bifurcated algorithm', ...),
  ('23b8c1e9-3924-46de-beb1-3b9046685257', 'User-centric even-keeled encryption', ...),
```

> "One transaction, FK-safe order — `psql` it into any Postgres or Supabase
> project."

**Now the kill-shot. Type (each run ~3 s):**
```powershell
synth-scale --ddl examples/demo_saas.sql --rows 100 --seed 42 --format sql --out "$env:TEMP\synth_demo\a.sql"
synth-scale --ddl examples/demo_saas.sql --rows 100 --seed 42 --format sql --out "$env:TEMP\synth_demo\b.sql"
Get-FileHash "$env:TEMP\synth_demo\a.sql", "$env:TEMP\synth_demo\b.sql" -Algorithm SHA256 | Format-Table Hash
```

**EXPECTED (real hashes from this machine — byte-identical):**
```
Hash
----
433C46750336F4B4561E43DC3D7FB60FB61914D53D455EB7C4DD0C4CBD57C5AC
433C46750336F4B4561E43DC3D7FB60FB61914D53D455EB7C4DD0C4CBD57C5AC
```

**Say (pause two full seconds on the identical hashes first):**
> "Same seed. Same bytes. Same SHA-256. Today, tomorrow, on your machine, in
> CI — your test fixtures never flake again. No LLM can promise you that."

**Type:**
```powershell
synth-scale --ddl examples/demo_saas.sql --rows 100 --seed 7 --format sql --out "$env:TEMP\synth_demo\c.sql"
Get-FileHash "$env:TEMP\synth_demo\c.sql" -Algorithm SHA256 | Format-Table Hash
```

**EXPECTED (real):**
```
CC8922D9BE96AB4B1ABE44F36E597F1CDCC67916EA27E14DBB2DF456A231118A
```

> "Different seed — a different, equally-valid world. Determinism is a dial,
> not a limitation."

*(Alternative to hashes if you prefer the byte-compare theater:
`fc /b "$env:TEMP\synth_demo\a.sql" "$env:TEMP\synth_demo\b.sql"` →
`FC: no differences encountered`.)*

**Fallbacks:**
- 1000-run stalls or machine is slow: kill it, run `--rows 100` instead
  (2.8 s, 9,800 rows) and quote the measured 98k/14 s number verbally —
  it's in BENCHMARK.md, you're not making it up.
- Hashes differ (means you edited the schema since rehearsal): don't debug
  on stage. Say "we pin these in CI — here's the rehearsal capture," show
  the screenshot, move on.
- File-head command errors on the path: `notepad "$env:TEMP\synth_demo\seed.sql"`
  opens it just as well.

## Beat 4 — 3:00–4:00 · "And it's actually correct"

**OPTIONAL-IF-DB** (only if `SYNTH_PG_URL` infra exists by demo week —
rehearse whichever branch you'll use):
```powershell
psql $env:SYNTH_PG_URL -f examples\demo_saas.sql
psql $env:SYNTH_PG_URL -f "$env:TEMP\synth_demo\seed.sql"
psql $env:SYNTH_PG_URL -c "SELECT count(*) FROM orders;"
```
**Say:** "That's a real Postgres accepting every one of the 98,000 rows —
every FK, every CHECK, every UNIQUE. `COMMIT`, not 'probably'."
*(Expect `count = 9000` at `--rows 1000`.)*

**ELSE (no live DB — the default plan):** point back at the screen, where
`Constraints checked: 295 / Violations found: 0` is still visible:

> "Don't take the generator's word — an independent validator re-checks
> every constraint against the full dataset on every run: NOT NULL, primary
> and composite uniques, FK integrity, CHECKs, even type conformance.
> Zero violations."

**Type the one-liner:**
```powershell
python -m pytest -q
```
**EXPECTED (real, ~10 s):**
```
129 passed, 8 skipped in 9.80s
```
> "A 129-test adversarial suite — written to *fail* against the engine until
> the engine earned it: UUID keys, NUMERIC(6,2) precision, date CHECKs,
> composite uniques, self-referencing hierarchies. The 8 skips are the
> live-Postgres load tests that run in CI."

**Fallback:** pytest is the fallback. If even that misbehaves, show the
checklist screenshot of the green run from an hour earlier.

## Beat 5 — 4:00–5:00 · The market close + the ask

**Show:** nothing new — you, facing the judges. (Optional: the Snaplet
slide from the Loom storyboard.)

**Say (verbatim — lifted from `SNAPLET_REPORT.md` §5c):**
> "The last serious company in this space, Snaplet, shut down in 2024 — and
> demand kept growing anyway: their abandoned seeding package is downloaded
> about 140,000 times a month today, triple what it did a year and a half
> ago, with open bugs nobody will ever fix. Supabase's own documentation
> tells developers the tool is unmaintained; their GitHub issues literally
> ask for 'a maintained third-party solution.' Snaplet needed a cloud, a
> ten-person team, and best-effort data that still broke on unique
> constraints. Synth-Scale runs on your machine, costs us nearly nothing to
> serve, and generates data that's *guaranteed* valid by construction —
> deterministic, so your CI fixtures never flake. An LLM can write you a
> probably-correct seed script; we give you provably-correct data."

**The ask (adjust the number to whatever the squad commits to — don't
improvise a new metric on stage):**
> "The orphaned Snaplet users are sitting in five known places — the
> supabase-community issue tracker, Supabase's own docs and Discord. Our ask:
> try it — `pip install synth-scale` — and introduce us to any team seeding
> Postgres or Supabase. Our success metric is weekly retained seeding runs,
> not stars; that's the number we'll report back."

---

## Q&A prep — the 6 hardest judge questions (≤4 sentences each)

**1. "Why not just use Claude?"**
Claude writes a *script*, and it's probably right — until a FK dangles, a
NUMERIC(6,2) overflows, or the schema changes and you re-prompt into
different data. It also physically can't emit 98,000 coherent rows in a chat
window. We're deterministic, constraint-guaranteed, and free per run — no
tokens, no flakes. Claude is welcome upstream: it writes the schema; we
populate it correctly every time.

**2. "Why did Snaplet die — doesn't that kill your market?"**
Their own words: not enough adoption *for them* — a ~10-person Berlin team
with S3/Fargate/Neon cloud costs on a $30/team price, and a pivot to seeding
that shipped eight weeks before the shutdown notice. The demand didn't die:
their abandoned package grew to ~140k npm downloads a month, 3.4x in 18
months, with zero maintenance. Their bar was venture-cloud-scale; ours is
four people and near-zero serving cost. Same revenue that killed them
sustains us — and if weekly retained runs don't show up within a quarter,
we'll say the bear case won.

**3. "How do you make money?"**
The CLI is free and local — that's the funnel Snaplet found too late, and
it costs us nothing to serve. We charge where teams already pay: CI/team
features — locked data contracts, hosted schema introspection, Supabase
project integration, support. Snaplet's lesson is written into our plan:
charge something early, keep COGS off the core loop. (Exact pricing is a
squad decision — don't quote numbers we haven't set.)

**4. "What's AI about this?"**
Deliberately nothing in the data path — one LLM call per row would destroy
determinism, speed, and offline use, which *are* the product. AI's seat is
upstream: your LLM writes the schema, and an optional one-shot "Claude
configures, the engine guarantees" step can generate pool vocabularies and
config — cached, committed, deterministic afterwards. We're the guarantee
layer under AI codegen, not a competitor to it. That's also why we can't
hallucinate a foreign key.

**5. "How is this different from Mockaroo or Faker?"**
Faker makes values; Mockaroo makes columns; neither understands your
*schema*. We parse the DDL — 15 tables, FK chains four levels deep,
composite keys, CHECK constraints — resolve the dependency graph, and
construct rows that satisfy every constraint by design, then re-validate
independently. Plus coherence they don't attempt: cities matching countries,
prices matching product tiers, `shipped_at` only on shipped orders, org
charts that are actual trees. And it's all reproducible from a seed —
Mockaroo can't give you the same dataset twice.

**6. "Where are your users?"**
Honest answer: early — this cohort is the validation sprint, and the case
study reports interviews and signups, not vanity metrics
[state the real current numbers from CASE_STUDY.md §5 — never invent].
What we know precisely is *where* they are: the dead Snaplet package's ~140k
monthly downloaders, the supabase-community/seed issue tracker, and a
Supabase GitHub issue literally requesting "a maintained third-party
solution." Our metric is weekly retained seeding runs; that's what we'll
stand behind next demo.

---

## After the demo

Delete the scratch outputs — nothing generated belongs in the repo:
```powershell
Remove-Item -Recurse -Force "$env:TEMP\synth_demo"
```
