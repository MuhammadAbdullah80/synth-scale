# Snaplet Intelligence Report

*Prepared for the Synth-Scale squad — 2026-07-19. Every non-obvious claim cited. Facts vs. inference labeled in §2.*

---

## 0. TL;DR — the 8 bullets the squad must know

1. **Snaplet died of low adoption, not a bad product.** Official words: *"we have not reached the necessary adoption levels to continue Snaplet"* — shutdown announced July 1, 2024, service off Aug 31, 2024. ([archived shutdown post](https://web.archive.org/web/20240716025550/https://www.snaplet.dev/post/snaplet-is-shutting-down))
2. **Two products, one company:** Snapshot (copy/subset/anonymize prod Postgres — the original, infra-heavy paid product) and Seed (`@snaplet/seed`, AI-assisted schema-aware seed generation — the 2023–24 pivot, launched as a polished standalone only ~2 months before the shutdown announcement).
3. **Demand is real and *growing* after death:** `@snaplet/seed` npm downloads grew from ~36k/month (Jan 2025) to ~123–140k/month (mid-2026) with effectively **zero maintenance since Aug 2024** ([npm API](https://api.npmjs.org/downloads/point/last-month/@snaplet/seed)). `@snaplet/copycat` does ~317k/month. People install a dead tool at 3.4x the rate they did 18 months ago.
4. **The community fork is a zombie.** `supabase-community/seed`: 787 stars, 24 open issues, last real code commit Aug 14, 2024; known breakage with Postgres identity columns, pgvector, unique constraints, Windows init. `snapshot` repo was **archived** Nov 2025. Supabase's own docs now say "treat it as an optional convenience."
5. **What killed it (our read):** tiny top-of-funnel (every HN launch ≤13 points), a $30/team/month price on an infra-heavy snapshot product (S3 + Fargate + Neon COGS), a too-late pivot to Seed, and free "good-enough" substitutes — `seed.sql` + faker + increasingly LLM-written seed scripts.
6. **Snaplet was genuinely better than us at DX:** `npx @snaplet/seed init` → fully **typed TypeScript client** from schema introspection, `(x) => x({min,max})` relationship fanout, `connect` to existing rows, Prisma/Drizzle/Supabase adapters, and **determinism via copycat** (hash-based faker: same input → same output). Steal list in §5a.
7. **The vacuum is being contested but not filled:** Neosync (nearest analog) was acquired by Grow Therapy and its repo archived Aug 30, 2025; Seedfast is a new paid entrant explicitly marketing "Snaplet Seed alternative" pages — and it openly concedes it **dropped determinism and the typed client**, the two things we lead with.
8. **User-acquisition goldmine locations:** `supabase-community/seed` issues, `supabase/supabase` issue #29890 ("docs recommend an unmaintained package"), the Supabase docs seeding page, r/Supabase, and Supabase's Discord (indexed on answeroverflow.com). These are people *already trying to seed Postgres* who hit a dead tool.

---

## 1. What Snaplet was

### Company facts

| | |
|---|---|
| Founded | 2021, HQ Berlin ([about-us, archived](https://web.archive.org/web/20240525083304/https://www.snaplet.dev/about-us)) |
| Founders | **Peter Pistorius** (CEO; RedwoodJS co-creator, ex-Chatterbug) + **Scott Chacon** (GitHub co-founder) as "Cofounder \| Advisor" ([about-us](https://web.archive.org/web/20240525083304/https://www.snaplet.dev/about-us)) |
| Team | ~8 (June 2022, [Heavybit podcast](https://www.heavybit.com/library/podcasts/jamstack-radio/ep-102-database-accessibility-with-peter-pistorius-of-snaplet)) → ~10 listed on about-us in 2024 |
| Funding | Seed round led by **boldstart ventures**, joined by base case capital, System.One, NP-Hard Ventures; 2021 angels incl. Tom Preston-Werner, **Supabase founders Paul Copplestone, Ant Wilson, Rory Wilding**, Netlify founders Matt Biilmann & Christian Bach, Zach Holman, Chad Fowler, David Mytton ([about-us](https://web.archive.org/web/20240525083304/https://www.snaplet.dev/about-us)); + Netlify Jamstack Innovation Fund ($100k program, [Netlify blog](https://www.netlify.com/blog/jamstack-innovation-fund-launches-with-the-10-most-promising-jamstack-startups/)). Total raised unclear — trackers disagree ([Tracxn](https://tracxn.com/d/companies/snaplet/__LyN3I9TXOnYDz5M15dPzgwxDSdyez0pVMoGOsP6yC-o) logs $100k; other aggregator data suggests ~$3.7M total). Treat "low-single-digit-millions seed" as the best estimate. |
| Died | Announced 2024-07-01; hosted service off **2024-08-31**; tech MIT-open-sourced; several engineers joined Supabase; Pistorius returned to Redwood (now RedwoodSDK) ([Supabase blog](https://supabase.com/blog/snaplet-is-now-open-source)) |

Note the investor list: **Supabase's founders were Snaplet angels.** The "Supabase rescued the code" arrangement was a personal-network favor, not a strategic acquisition — which explains the minimal follow-through (§3).

### Product #1: Snapshot (2021 → 2024) — "a better database dump"

The original thesis, per Pistorius: *"you want to build features or fix bugs and write tests that reflect reality, and creating a seed script isn't part of any of that work"* ([Heavybit, June 2022](https://www.heavybit.com/library/podcasts/jamstack-radio/ep-102-database-accessibility-with-peter-pistorius-of-snaplet)). So: connect to **production** Postgres, then:

- **Capture** — a cloud Fargate worker dumps the DB to S3 (*"we copy that data to disk where you can then restore it using our CLI onto your local machine, onto a staging database"*).
- **Subset** — shrink the snapshot while keeping referential integrity by *"traversing tables, selecting all the rows that are connected"* ([Supabase blog](https://supabase.com/blog/snaplet-is-now-open-source)).
- **Transform / anonymize** — JavaScript transformations per column; PII columns replaced via **copycat**, their deterministic faker (*"for any given input it'll always produce the same output"* — hash the input, index into a value list). Repo: [snaplet/copycat](https://github.com/snaplet/copycat), 1,053 stars.
- **Restore** — `npx snaplet snapshot restore` locally or in CI.
- Later add-ons: **Preview Databases** (instant Neon-backed DB per git branch / Netlify preview, 2023), VS Code extension, a "Proxy" feature.

Docs promised the full workflow trio: local dev, E2E testing in CI/CD, and preview environments — "composable tooling for developers to manage the data in any development environment" ([homepage, archived Mar 2024](https://web.archive.org/web/20240324210117/https://www.snaplet.dev/)).

### Product #2: Seed (`@snaplet/seed`, 2023 → 2024) — "a better seed script"

For devs *not* authorized to touch prod (their own homepage decision tree literally asked "Am I authorized to use production credentials? → NO → Seed"). Timeline: Show HN of a seed feature Oct 2023 (13 points); standalone `snaplet/seed` repo created **Feb 5, 2024**; Product Hunt launch **May 6, 2024** — 485 upvotes, #2 of the day ([Product Hunt](https://www.producthunt.com/products/snaplet-seed)); company shutdown announced **8 weeks later**.

How it worked ([docs mirror](https://snaplet-seed.netlify.app/seed/getting-started/overview), [repo README](https://github.com/supabase-community/seed)):

1. `npx @snaplet/seed init` — introspects your DB (Postgres/SQLite/MySQL; adapters for Prisma, Drizzle, Supabase, node-postgres, better-sqlite3) and **generates a fully typed TypeScript client** synced to your schema (`npx @snaplet/seed sync` after migrations).
2. You write a seed script against that client:
   - `await seed.posts((x) => x(5))` — 5 posts, FKs auto-satisfied;
   - `await seed.organizations([{ name: 'Snaplet', members: (x) => x({ min: 1, max: 10 }) }])` — **relationship fanout**, nested;
   - `connect: true` / connect callbacks — link generated rows to existing pools instead of creating new parents.
3. **Values**: copycat-based deterministic generation by default (same seed → same DB), plus shipped datasets (countries, currencies). **AI layer**: column-content inference via OpenAI or Groq/Llama3, with predictions cached to `.snaplet/dataExamples.json` so runs stay reproducible and editable ([README](https://github.com/supabase-community/seed)). Notably, the AI part was added to the open-source repo only in **July 2024** (commit "Added AI data generation using ChatGPT and Groq(llama3)", #181) — before that it ran through Snaplet's hosted API (commit #180: "Remove reliance on API").
4. Marketed for local dev **and** deterministic test fixtures in CI.

### Pricing (Snapshot-era, archived Dec 2023 — [pricing page](https://web.archive.org/web/20231213143623/https://www.snaplet.dev/pricing))

| Plan | Price | Included |
|---|---|---|
| Free | $0 | 1 GB snapshot storage, 5 h snapshot compute, 2 GB transfer, 10 h preview-DB usage /mo |
| Pro | **$30 / team / month** (not per seat) + opt-in usage overages | 10 GB storage, 50 h compute, 20 GB transfer, 100 h preview DB |
| Enterprise | Custom | Volume plans, SLAs |

Their own FAQ spelled out the COGS: S3 storage, Fargate capture workers, Neon compute for preview DBs. Self-hosting was allowed and documented — i.e., the paid product competed with a free version of itself.

---

## 2. Product autopsy — why it shut down

### VERIFIED (stated or directly observable)

- **Official cause = adoption.** *"While we've helped many developers since then, we have not reached the necessary adoption levels to continue Snaplet."* — Peter Pistorius, July 1, 2024 ([archived post](https://web.archive.org/web/20240716025550/https://www.snaplet.dev/post/snaplet-is-shutting-down)). No blame placed on funding, competition, or AI in any public statement. Supabase's post adds only that startups are hard; Pistorius: *"Although the company is closing, my belief remains strong, so we are open-sourcing the tools we've built"* ([Supabase blog](https://supabase.com/blog/snaplet-is-now-open-source)).
- **Top-of-funnel was measurably tiny.** Verified via HN Algolia: Show HN "Snaplet Seed" May 2024 — **5 points, 0 comments**; Show HN Oct 2023 — 13 points; the shutdown post — 4 points, 1 comment; the Supabase open-sourcing post — 4 points, 1 comment. A company in HN's core demographic never once cracked the front page. (Product Hunt was their best channel: 485 votes.)
- **Free during the entire early growth phase** — still "free open beta" with a team of 8 in mid-2022, a year in; monetization was explicitly deferred to "later, teams/enterprise" ([Heavybit](https://www.heavybit.com/library/podcasts/jamstack-radio/ep-102-database-accessibility-with-peter-pistorius-of-snaplet)).
- **The Seed pivot came very late.** Standalone repo Feb 2024, polished launch May 6, 2024, shutdown notice July 1, 2024. Verified from repo creation date and PH launch date.
- **Infra-heavy cost structure on the paid product** (S3/Fargate/Neon, per their own pricing FAQ) against a $30/team flat price with opt-in-only overages and a free self-host path.
- **Category-wide weakness, not just Snaplet:** Neosync — the closest surviving analog (anonymize prod + synthetic data for Postgres) — sold to Grow Therapy for its privacy team, repo archived Aug 30, 2025, cloud offline ([pulse2](https://pulse2.com/grow-therapy-acquires-data-privacy-company-neosync/), [repo](https://github.com/nucleuscloud/neosync)).

### INFERENCE (our read — labeled, not stated by anyone at Snaplet)

- **[Inference] The wedge was a vitamin priced like a painkiller.** Snapshot's buyer needed (a) a prod DB with sensitive data, (b) a team, (c) willingness to route prod data through a third-party cloud. That's a compliance-anxious mid-market slice, and the security-conscious half of it self-hosted for free. $30/team/month can't carry S3+Fargate+Neon COGS plus a 10-person Berlin team unless adoption is enormous. It wasn't.
- **[Inference] Free substitutes bounded the price of both products.** pg_dump + scrubbing scripts, pgsync/replibyte for snapshots; faker + a hand-written `seed.sql` for seeding. By 2023–24, Copilot/ChatGPT could write a "probably-correct" seed script in one prompt — collapsing the perceived value of a paid seeding SaaS. No founder said "LLMs killed us," but the seed pivot itself *added* an LLM (their AI column inference) — they were converging on the same conclusion from the other side.
- **[Inference] The pivot was right but ~18 months late.** Seed had the better funnel (no prod credentials needed, `npx` to value in minutes, PH #2 of the day) and near-zero COGS once local. Had Seed been the 2022 product, the story might differ. By May 2024 the runway math was presumably already fatal — 8 weeks from flagship launch to shutdown notice means the decision was effectively made before the launch.
- **[Inference] Seed also never had a monetization story.** It was a client-side npm package whose only cloud tether was the AI-values API (removed during open-sourcing). Nothing on the archived pricing page charges for Seed at all. The pivot fixed the funnel but not the business.
- **[Inference] Angel-heavy cap table + modest seed = soft landing incentive.** With Supabase and Netlify founders as angels, an aqui-absorb into Supabase + open-source hand-off was the graceful exit, and the shutdown notes read like exactly that.

---

## 3. What happened after (state as of July 2026)

### The arrangement

Supabase announced (Aug 14, 2024) that three tools go MIT and migrate to Supabase orgs: **copycat**, **seed**, **snapshot**; ex-Snaplet engineers at Supabase would "pick up the ongoing maintenance"; tools stay decoupled from Supabase ([blog](https://supabase.com/blog/snaplet-is-now-open-source)). Snapshot users were pointed to self-hosting ([supabase-community/snapshot](https://github.com/supabase-community/snapshot)).

### Repo reality check (via GitHub API, 2026-07-19)

| Repo | Stars | Open issues | Last real activity | Status |
|---|---|---|---|---|
| [supabase-community/seed](https://github.com/supabase-community/seed) | 787 | 24 | Code: **Aug 14, 2024** (README edit); one repo-hygiene push May 2026 | Alive in name only. v0.98.0 (Jul 30, 2024) is still the latest release |
| [supabase-community/snapshot](https://github.com/supabase-community/snapshot) | 325 | 12 | — | **Archived Nov 2025** |
| [supabase-community/copycat](https://github.com/supabase-community/copycat) | 1,053 | — | Jan 2025 | Dormant but stable (it's a leaf library; ~317k npm downloads/mo) |
| snaplet/docs | 29 | — | Sep 2024 | Dead; docs.snaplet.dev offline, mirrors live at snaplet-seed.netlify.app / snaplet-snapshot.netlify.app |

### Is seed usable today?

Partially. It works on plain schemas, but open, unfixed issues include: **Postgres identity columns broken** (#199), **pgvector columns mishandled** (#206), **false unique-constraint failures** (#191, #195, #207), **generated columns** (#195), **`init` broken on Windows 11** (#193), no Ollama/custom-endpoint AI (#198, #204), npm-audit vulnerabilities in v0.98.0's deps (#213), and a required `sync` regen step after every schema migration. Issues get 0–9 comments and no maintainer resolution.

### Demand signals (the goldmine)

- **npm downloads grew ~3.4x post-mortem**: `@snaplet/seed` ~36k/mo (Jan 2025) → ~123k/mo (Apr–Jun 2026), 139,522 in the last 30 days ([npm API](https://api.npmjs.org/downloads/point/last-month/@snaplet/seed)). Caveat: partly driven by Supabase's docs still featuring it and by CI re-installs — but that *is* the funnel.
- **Supabase's own docs are an open wound**: the [seeding guide](https://supabase.com/docs/guides/local-development/seeding-your-database) now hedges ("community-maintained… occasional fixes… treat it as an optional convenience"), and [supabase/supabase#29890](https://github.com/supabase/supabase/issues/29890) explicitly asks Supabase to either build seeding into the CLI or "recommend a maintained third-party solution." **That issue is a literal request for our product.**
- **Where the orphaned users congregate** (ranked for acquisition): 1) `supabase-community/seed` issue tracker; 2) `supabase/supabase` issues/discussions (e.g. [seeding auth.users #35391](https://github.com/orgs/supabase/discussions/35391)); 3) Supabase Discord (indexed publicly via answeroverflow.com — searchable "seed" threads); 4) r/Supabase and r/webdev; 5) Prisma/Drizzle communities (seed's adapters left users there too).
- **Someone else noticed first**: [Seedfast](https://seedfa.st/blog/snaplet-seed-alternative) runs comparison/migration pages targeting both Snaplet and Neosync refugees — plain-English prompts ("10 users with 5 orders each"), live schema reads, MCP integration, 30-day trial. Crucially their own copy admits users "lose determinism (fresh data per run vs. reproducible outputs) and the typed TypeScript API." They validated the vacuum *and* left our exact differentiators on the table.

---

## 4. Feature X-ray: Snaplet Seed vs. Synth-Scale

| Capability | Snaplet Seed (@snaplet/seed, v0.98) | Synth-Scale (current/planned) | Who wins |
|---|---|---|---|
| Schema introspection | Yes — Postgres/SQLite/MySQL + Prisma/Drizzle ORM-level introspection | Yes — paste schema / Postgres+Supabase focus | Tie on PG; they covered more engines/ORMs |
| Typed seed client / codegen | **Yes — flagship.** Generated TS client, autocomplete, IDE docs | No (CLI + web) | **Snaplet** — biggest DX gap to close |
| Relationship fanout | **Yes** — `(x) => x({min,max})`, nested plans, elegant | Yes — deterministic fanout in engine, but no comparable authoring syntax | Snaplet on ergonomics, us on guarantees |
| Connect to existing rows | Yes — `connect` pools | Planned/partial | Snaplet |
| Determinism | Yes for base values (copycat hash-based) + cached AI examples; but **no global run-level guarantee** — vitest/ordering issues (#178), constraint failures break runs nondeterministically | **Core guarantee** — seeded PRNG, reproducible full-DB output | **Us** (their story was good, ours is the product) |
| Constraint correctness | Best-effort; FKs auto-satisfied, but uniques/checks/identity/generated columns produced **runtime failures** (#191/#195/#199/#207) | **By construction** + validator pass | **Us** — this is their top bug category; lead with it |
| Realism/coherence | AI column inference (OpenAI/Groq) + curated datasets; column-level only | Coherence layer: cross-row/cross-table consistency | Us on cross-field coherence; adopt their cached-examples pattern |
| CI usage | Yes, documented; needed `sync` regen per migration (drift complaints) | Yes — deterministic = diffable fixtures | Us, if we keep zero-codegen or auto-sync |
| Supabase specifics (auth.users etc.) | Adapter existed; seeding auth.users with usable password hashes still an open ask (#208, discussion #35391) | Opportunity: ship this working | Open lane |
| Ecosystem/DX polish | `npx init` onboarding, PH-grade landing, Prisma auto-detect, VS Code ext (snapshot era) | Early | **Snaplet** — the inspiration list |
| Prod-data snapshot/anonymize | Separate product (Snapshot/copycat) | Out of scope | N/A — deliberately not our fight (it's the part with the worst COGS and compliance surface) |

**Honest summary:** Snaplet beat our current state on onboarding, typed-client DX, ORM adapters, and authoring syntax. We beat Snaplet (and Seedfast) on the two things their issue tracker proves users actually break on: **guaranteed constraint-valid output** and **true run-level determinism**.

---

## 5. Lessons for Synth-Scale

### (a) STEAL

| What | Why | Effort | Where it fits |
|---|---|---|---|
| `(x) => x(n)` / `x({min,max})` fanout syntax (or a YAML/CLI equivalent: `users: 100, posts: 5..20 per user`) | The single most-loved piece of their DX; makes "thousands of perfectly-linked rows" expressible in one line | **S** | CLI plan format / web UI row-count controls |
| `connect`-to-existing-rows semantics | Real projects seed *into* non-empty DBs (esp. Supabase auth.users) | **M** | Engine: reference pools alongside generated pools |
| Cached AI-examples file (`.snaplet/dataExamples.json` pattern) | Lets us use an LLM for column *vocabulary* once, commit the cache, and stay 100% deterministic afterwards — "AI-informed, deterministically executed" is our pitch in one file | **S** | Coherence layer input; check the cache into the user's repo |
| `npx <tool> init` single-command onboarding with DB/ORM auto-detect (they auto-detected Prisma) | Their PH launch worked because time-to-value was minutes | **M** | CLI |
| Typed client / typegen (TS types for generated data, even just row-type exports) | Flagship Snaplet feature; TS-first Supabase crowd expects it | **L** | Post-MVP roadmap; start with generated `.d.ts` for output shape, not a full fluent client |
| Supabase `auth.users` + storage seeding that actually logs in (hashed passwords) | Open unmet ask (#208, discussion #35391) — instant "it does the thing Snaplet never fixed" moment | **M** | Supabase adapter |
| Their curated datasets idea (countries, currencies, etc.) via copycat — which is MIT and still 1k-stars/317k-dl healthy | Don't rebuild faker; **consider depending on or vendoring `@snaplet/copycat` itself** — deterministic by design, battle-tested, aligned with our engine | **S** | Value-generation layer |
| Docs with a decision tree ("what data problem do you have → tool") | Their docs' use-case framing (local dev / E2E / debugging) converted well | **S** | Docs/site |

### (b) AVOID

- **Don't defer monetization for years.** Snaplet was free in open beta for ~1.5 years with 8 salaries; when pricing arrived it was $30/team with opt-in overages and a free self-host path. Charge something early, even if small, to find out who values it.
- **Don't build COGS-heavy cloud into the core loop.** Fargate/S3/Neon per-customer costs on a $30 flat plan is upside-down. Our capped web + local CLI model is right; keep heavy generation on the user's machine or hard-capped.
- **Don't ship the flagship pivot with 8 weeks of runway.** If a pivot is right, make it *the* product while there's still a year to compound. Corollary for us: don't split focus across two products (their Snapshot/Seed split diluted a 10-person team).
- **Don't rely on codegen that drifts.** The `sync`-after-every-migration step is the #1 migration-pain theme in alternative-seeking threads. Either introspect live at run time or auto-sync invisibly.
- **Don't confuse launch applause with adoption.** 485 PH upvotes, 2nd of the day — dead 12 weeks later. Track retained weekly seeding runs, not stars.
- **Don't let "best-effort correctness" ship.** Their most-commented bugs were unique/identity/generated-column failures — i.e., the generator producing invalid data. That's precisely the failure mode our by-construction engine exists to make impossible; never regress into "usually valid."

### (c) POSITIONING

**How to talk about the vacuum (without overclaiming):** Name the facts, not the cause: Snaplet shut down Aug 2024; the community fork has had no meaningful release since v0.98.0 (Jul 2024); Neosync was archived Aug 2025; Supabase's own docs call the survivor "an optional convenience." Meanwhile the dead package still gets ~140k npm downloads a month. That's demonstrated, growing demand with no maintained supply. Do **not** claim Snaplet "proved the market" — their own words are that adoption was insufficient for a *10-person venture-backed cloud company*. It may well be sufficient for a lean product with near-zero COGS.

**Liftable Demo Day paragraph:**
> "The last serious company in this space, Snaplet, shut down in 2024 — and demand kept growing anyway: their abandoned seeding package is downloaded about 140,000 times a month today, triple what it did a year and a half ago, with open bugs nobody will ever fix. Supabase's own documentation tells developers the tool is unmaintained; their GitHub issues literally ask for 'a maintained third-party solution.' Snaplet needed a cloud, a ten-person team, and best-effort data that still broke on unique constraints. Synth-Scale runs on your machine, costs us nearly nothing to serve, and generates data that's *guaranteed* valid by construction — deterministic, so your CI fixtures never flake. An LLM can write you a probably-correct seed script; we give you provably-correct data."

**The "market's not there" risk — honestly assessed.** The bear case is strong and we should not pretend otherwise: Snaplet's *stated* cause of death was insufficient adoption; every HN launch flopped; Neosync also exited; nobody in this niche has yet built a big business. The steelman rebuttal, with receipts: (1) Snaplet's adoption bar was a Berlin team of ~10 plus AWS/Neon COGS — our bar is 4 people and capped infra; the same revenue that kills them sustains us. (2) The demand curve *inverted* after their death — 3.4x npm growth for an unmaintained package is organic pull, not marketing. (3) Their funnel was throttled by the prod-credentials requirement (Snapshot) and a late pivot; ours starts at "paste your schema." (4) A funded competitor (Seedfast) just entered the same vacuum, which is third-party confirmation the demand is worth chasing. Net: the Snaplet story says *the venture-scale cloud version* of this business failed; it says nothing bad — and several encouraging things — about a lean, correctness-first tool. But if weekly retained usage doesn't materialize within a quarter of launch, the bear case was right, and we should say so out loud now.

---

## Appendix: Source index

- Shutdown post (archived): https://web.archive.org/web/20240716025550/https://www.snaplet.dev/post/snaplet-is-shutting-down
- Supabase open-sourcing post: https://supabase.com/blog/snaplet-is-now-open-source
- Pricing (archived Dec 2023): https://web.archive.org/web/20231213143623/https://www.snaplet.dev/pricing
- Homepage (archived Mar 2024): https://web.archive.org/web/20240324210117/https://www.snaplet.dev/
- About us / team / investors (archived May 2024): https://web.archive.org/web/20240525083304/https://www.snaplet.dev/about-us
- Seed repo: https://github.com/supabase-community/seed · Snapshot (archived): https://github.com/supabase-community/snapshot · Copycat: https://github.com/snaplet/copycat
- Seed docs mirror: https://snaplet-seed.netlify.app/seed/getting-started/overview
- Supabase seeding docs: https://supabase.com/docs/guides/local-development/seeding-your-database
- Docs-are-dead issue: https://github.com/supabase/supabase/issues/29890 · auth.users seeding ask: https://github.com/orgs/supabase/discussions/35391
- HN threads: shutdown https://news.ycombinator.com/item?id=40844298 (4 pts) · open-source https://news.ycombinator.com/item?id=41244171 (4 pts) · Show HN Seed https://news.ycombinator.com/item?id=40275677 (5 pts)
- Heavybit podcast (Pistorius, Jun 2022): https://www.heavybit.com/library/podcasts/jamstack-radio/ep-102-database-accessibility-with-peter-pistorius-of-snaplet
- Product Hunt launch: https://www.producthunt.com/products/snaplet-seed
- npm stats: https://api.npmjs.org/downloads/point/last-month/@snaplet/seed
- Funding trackers: https://tracxn.com/d/companies/snaplet/__LyN3I9TXOnYDz5M15dPzgwxDSdyez0pVMoGOsP6yC-o · https://www.crunchbase.com/organization/snaplet
- Neosync exit: https://pulse2.com/grow-therapy-acquires-data-privacy-company-neosync/ · https://github.com/nucleuscloud/neosync
- Seedfast (new competitor targeting the vacuum): https://seedfa.st/blog/snaplet-seed-alternative
