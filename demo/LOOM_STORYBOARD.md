# Synth-Scale — Loom Storyboard (2:30 target, 3:00 hard cap)

One continuous screen recording (terminal + one editor pane), voiceover
recorded in one take. Terminal: dark theme, font ≥ 18 pt, window maximized.
Working dir: `A:\comebck\datagen\datagen_pkg`, venv activated. Pre-type every
command into shell history so on camera it's ↑ + Enter.

Voice: first-person plural, calm, no filler. Every number below is measured
(see `demo/BENCHMARK.md`) — if you re-record on another machine, re-measure
and swap the numbers.

---

## Shot list

### Shot 1 — 0:00–0:20 · The problem (editor pane)

**On screen:** `examples/demo_saas.sql` scrolling slowly — 15 CREATE TABLEs,
pause on `subscriptions` (the CHECK constraints) and `order_items` (composite
PK of two FKs).

**VO:** "This is a real SaaS schema — fifteen tables, foreign keys four
levels deep, check constraints, composite keys. Every app starts with a
database like this, and it starts empty. You can ask Claude to write you a
seed script — you'll get something *probably* correct: broken foreign keys,
numeric overflows, different data every run. We built the guaranteed
version."

### Shot 2 — 0:20–0:55 · Instant gratification (Beat 2 in video form)

**On screen:** type
`synth-scale --ddl examples/demo_saas.sql --rows 50 --preview`
Let the summary table land, then scroll slowly through `users`, then
`products`.

**VO:** "One command — synth-scale, point it at the schema, preview. No LLM,
no cloud, no config. Watch the data, not just the shape of it: Diego is
male, Priya is female; Berlin sits in Germany. Wireless Earbuds Pro is
Electronics, mid-tier, priced like mid-tier — twenty-five ninety-nine,
because real prices end in ninety-nine. Statuses agree with timestamps: a
cancelled order has a cancelled-at and no shipped-at. And the validator just
re-checked all 295 constraints — zero violations."

**Cut discipline:** do NOT scroll every table; users → products → the
`Violations found: 0` line, done.

### Shot 3 — 0:55–1:40 · The mic-drop: scale + determinism (Beat 3)

**On screen:** run
`synth-scale --ddl examples/demo_saas.sql --rows 1000 --seed 42 --format sql --out "$env:TEMP\synth_demo\seed.sql"`
— speed up the ~14 s wait 2x in edit, keep the summary table full-speed.
Then `Get-Content "$env:TEMP\synth_demo\seed.sql" -TotalCount 6`.

**VO:** "Now the real thing: ninety-eight thousand rows across all fifteen
tables, foreign-key-safe insert order, in about fourteen seconds on a
laptop — zero constraint violations. It's one SQL file; psql it into any
Postgres or Supabase project."

**On screen:** the determinism cut — run the seed-42 generation twice into
`a.sql` and `b.sql`, then `Get-FileHash` both; hold on the two identical
hashes for a full 2 seconds; then one run with `--seed 7` and the different
hash.

**VO:** "And here's the part no LLM can give you: same seed — byte-identical
file, same hash, today, tomorrow, in CI. Different seed, different data.
Your test fixtures stop flaking, forever."

### Shot 4 — 1:40–2:05 · Proof it's actually correct

**On screen:** `python -m pytest -q` finishing with `129 passed`, then a
quick cut of `tests/test_hardening.py` header comment ("measures the ENGINE,
not the fixture").

**VO:** "We don't trust ourselves either. An independent validator re-checks
every constraint on every run, and a 129-test adversarial suite was written
to fail against the engine until the engine earned it — UUID keys, numeric
precision, date checks, composite uniques, self-referencing hierarchies."

*(If a live Postgres is available at record time, replace the pytest cut
with the psql load from DEMO_SCRIPT Beat 4 — a real `COMMIT` beats a test
count.)*

### Shot 5 — 2:05–2:30 · The market close (Beat 5)

**On screen:** static slide (one image, no motion): left — Snaplet shutdown
notice + "~140k npm downloads/month, 18 months after death"; right — the
one-liner "Claude writes you a probably-correct seed script. We give you
guaranteed-correct data — first run, every run." + `pip install synth-scale`.

**VO:** "The last company in this space, Snaplet, shut down in 2024 — and
their abandoned package is still downloaded about a hundred and forty
thousand times a month, triple what it did before they died. Supabase's own
docs call it unmaintained. Snaplet needed a cloud and a ten-person team and
still shipped best-effort data. Synth-Scale runs on your machine, costs
nearly nothing to serve, and the data is guaranteed valid by construction.
We're Synth-Scale. First run, every run."

---

## Edit notes

- Total speech ≈ 300 words ≈ 2:10 at normal pace; leaves 20 s of breathing
  room for the pauses on the hash shot and the `Violations found: 0` line.
- The only speed-ups are the 14 s generation wait (Shot 3) and pytest run
  (Shot 4). Never speed up while text the viewer should read is on screen.
- Captions on: the two hash values and "0 violations" must be readable on
  mute — most Loom views are muted.
- Record 16:9, 1080p minimum; zoom terminal to ≥ 18 pt BEFORE recording, not
  in post.
- Cleanup after recording: delete `$env:TEMP\synth_demo\` — don't leave
  generated SQL in the repo or the recording folder.
