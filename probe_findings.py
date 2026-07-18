# -*- coding: utf-8 -*-
"""Empirical probes for suspected defects in datagen. Run from datagen_pkg/."""
import sys, traceback

from datagen.ddl_parser import parse_ddl
from datagen.engine import EngineConfig, run_engine
from datagen.validator import validate

RESULTS = []

def probe(name):
    def deco(fn):
        try:
            out = fn()
            RESULTS.append((name, out))
            print(f"\n### {name}\n{out}")
        except Exception as e:
            RESULTS.append((name, f"EXCEPTION: {type(e).__name__}: {e}"))
            print(f"\n### {name}\nEXCEPTION: {type(e).__name__}: {e}")
            traceback.print_exc()
        return fn
    return deco


@probe("P1: UUID PK determinism (README: same seed = byte-identical)")
def p1():
    ddl = """
    CREATE TABLE accounts (
      id UUID PRIMARY KEY,
      email VARCHAR(120) UNIQUE NOT NULL
    );
    """
    s = parse_ddl(ddl)
    r1 = run_engine(s, {"accounts": 3}, EngineConfig(seed=7))
    r2 = run_engine(parse_ddl(ddl), {"accounts": 3}, EngineConfig(seed=7))
    ids1 = [r["id"] for r in r1["accounts"]]
    ids2 = [r["id"] for r in r2["accounts"]]
    return f"run1 ids={ids1}\nrun2 ids={ids2}\nIDENTICAL={ids1 == ids2}"


@probe("P2: NUMERIC(p,s) precision overflow (NUMERIC(4,2) max is 99.99)")
def p2():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      rate NUMERIC(4,2)
    );
    """
    s = parse_ddl(ddl)
    col = s.tables["t"].get_column("rate")
    g = run_engine(s, {"t": 8}, EngineConfig(seed=3, null_rate=0.0))
    vals = [r["rate"] for r in g["t"]]
    over = [v for v in vals if v is not None and abs(v) >= 100]
    return (f"parsed precision={col.precision} scale={col.scale}\n"
            f"values={vals}\nvalues >= 100 (would raise numeric field overflow): {over}")


@probe("P3: date CHECK with date literals (BETWEEN)")
def p3():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      d DATE CHECK (d BETWEEN '2020-01-01' AND '2020-12-31')
    );
    """
    s = parse_ddl(ddl)
    chk = s.tables["t"].check_constraints
    parsed = [c.parsed for c in chk]
    g = run_engine(s, {"t": 5}, EngineConfig(seed=3, null_rate=0.0))
    vals = [str(r["d"]) for r in g["t"]]
    return f"parsed={parsed}\ngenerated dates={vals}\n(bound respected? expect all in 2020)"


@probe("P4: date lower-bound CHECK (>=) ignored?")
def p4():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      d DATE CHECK (d >= '2030-01-01')
    );
    """
    s = parse_ddl(ddl)
    parsed = [c.parsed for c in s.tables["t"].check_constraints]
    g = run_engine(s, {"t": 5}, EngineConfig(seed=3, null_rate=0.0))
    vals = [str(r["d"]) for r in g["t"]]
    rep = validate(s, g, seed=3)
    return (f"parsed={parsed}\ngenerated={vals}\n"
            f"violations_found={rep.violations_found}\nerrors={rep.errors}\nwarnings={rep.warnings}")


@probe("P5: composite UNIQUE mixing FK + non-FK column")
def p5():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, email VARCHAR(120) UNIQUE NOT NULL);
    CREATE TABLE posts (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id),
      slug VARCHAR(10) NOT NULL,
      UNIQUE (user_id, slug)
    );
    """
    s = parse_ddl(ddl)
    uc = s.tables["posts"].unique_constraints
    g = run_engine(s, {"users": 2, "posts": 40}, EngineConfig(seed=5, null_rate=0.0))
    keys = [(r["user_id"], r["slug"]) for r in g["posts"]]
    dupes = len(keys) - len(set(keys))
    rep = validate(s, g, seed=5)
    return (f"unique_constraints parsed={uc}\nduplicate (user_id,slug) pairs={dupes}\n"
            f"validator violations={rep.violations_found}\nerrors={rep.errors[:3]}")


@probe("P6: two-col CHECK where left is already generated (PK/FK)")
def p6():
    ddl = """
    CREATE TABLE t (
      id INT PRIMARY KEY,
      lo INT NOT NULL,
      CONSTRAINT chk CHECK (id > lo)
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 5}, EngineConfig(seed=5, null_rate=0.0))
    rows = [(r["id"], r["lo"], r["id"] > r["lo"]) for r in g["t"]]
    rep = validate(s, g, seed=5)
    return f"(id, lo, satisfies id>lo)={rows}\nvalidator violations={rep.violations_found}"


@probe("P7: derived column type for INTEGER two-col CHECK")
def p7():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      min_qty INT NOT NULL,
      max_qty INT NOT NULL,
      CONSTRAINT chk CHECK (max_qty > min_qty)
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 5}, EngineConfig(seed=5, null_rate=0.0))
    rows = [(r["min_qty"], r["max_qty"], type(r["max_qty"]).__name__) for r in g["t"]]
    return f"(min_qty, max_qty, type of max_qty)={rows}\nINT column holding float => load error"


@probe("P8: NOT NULL left column derived from NULLable right column")
def p8():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      start_date DATE,
      end_date DATE NOT NULL,
      CONSTRAINT chk CHECK (end_date > start_date)
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 40}, EngineConfig(seed=5, null_rate=0.3))
    nulls = sum(1 for r in g["t"] if r["end_date"] is None)
    rep = validate(s, g, seed=5)
    return (f"NULLs in NOT NULL end_date: {nulls}/40\n"
            f"validator violations={rep.violations_found}\nfirst errors={rep.errors[:2]}")


@probe("P9: strict inequality on non-integer bound (x > 2.5)")
def p9():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      score INT CHECK (score > 2.5)
    );
    """
    s = parse_ddl(ddl)
    parsed = [c.parsed for c in s.tables["t"].check_constraints]
    g = run_engine(s, {"t": 8}, EngineConfig(seed=5, null_rate=0.0))
    vals = sorted(r["score"] for r in g["t"])
    bad = [v for v in vals if v <= 2.5]
    return f"parsed={parsed}\nvalues(min 5)={vals[:5]}\nviolating (<=2.5)={bad}"


@probe("P10: is check_eval (generate-and-filter fallback) wired into generation?")
def p10():
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "import datagen.engine as e, inspect; src=inspect.getsource(e); "
         "print('check_eval referenced in engine:', 'check_eval' in src or 'evaluate_check' in src)"],
        capture_output=True, text=True)
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      a INT NOT NULL,
      b INT NOT NULL,
      CONSTRAINT chk CHECK (a + b < 10)
    );
    """
    s = parse_ddl(ddl)
    parsed = [c.parsed for c in s.tables["t"].check_constraints]
    g = run_engine(s, {"t": 10}, EngineConfig(seed=5, null_rate=0.0))
    rows = [(r["a"], r["b"], r["a"] + r["b"] < 10) for r in g["t"]]
    rep = validate(s, g, seed=5)
    return (f"{out.stdout.strip()}\nparsed(exotic check)={parsed}\n"
            f"(a,b,satisfies a+b<10)={rows[:5]}\n"
            f"validator violations={rep.violations_found}\nerrors={rep.errors[:1]}")


@probe("P11: created_at / updated_at chronological coherence")
def p11():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      created_at TIMESTAMP NOT NULL,
      updated_at TIMESTAMP NOT NULL
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 10}, EngineConfig(seed=5, null_rate=0.0))
    bad = sum(1 for r in g["t"] if r["updated_at"] < r["created_at"])
    return f"rows where updated_at < created_at: {bad}/10 (no CHECK in DDL to catch it)"


@probe("P12: cross-table chronology (child.created_at vs parent.created_at)")
def p12():
    ddl = """
    CREATE TABLE users (id SERIAL PRIMARY KEY, created_at TIMESTAMP NOT NULL);
    CREATE TABLE orders (
      id SERIAL PRIMARY KEY,
      user_id INT NOT NULL REFERENCES users(id),
      created_at TIMESTAMP NOT NULL
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"users": 5, "orders": 20}, EngineConfig(seed=5, null_rate=0.0))
    ucreated = {r["id"]: r["created_at"] for r in g["users"]}
    bad = sum(1 for r in g["orders"] if r["created_at"] < ucreated[r["user_id"]])
    return f"orders created BEFORE their user existed: {bad}/20"


@probe("P13: multiple FKs on same child->parent edge inside a cycle")
def p13():
    ddl = """
    CREATE TABLE a (
      id SERIAL PRIMARY KEY,
      b_id INT
    );
    CREATE TABLE b (
      id SERIAL PRIMARY KEY,
      primary_a_id INT,
      backup_a_id INT
    );
    ALTER TABLE a ADD CONSTRAINT fk_a_b FOREIGN KEY (b_id) REFERENCES b(id);
    ALTER TABLE b ADD CONSTRAINT fk_b_a1 FOREIGN KEY (primary_a_id) REFERENCES a(id);
    ALTER TABLE b ADD CONSTRAINT fk_b_a2 FOREIGN KEY (backup_a_id) REFERENCES a(id);
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"a": 5, "b": 5}, EngineConfig(seed=5))
    rep = validate(s, g, seed=5)
    return (f"validator violations={rep.violations_found}\nerrors={rep.errors[:3]}")


@probe("P14: NOT NULL column with a DEFAULT and no CHECK -> ok? / enum validator coverage")
def p14():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      status VARCHAR(20) CHECK (status IN ('a','b'))
    );
    """
    s = parse_ddl(ddl)
    col = s.tables["t"].get_column("status")
    chks = s.tables["t"].check_constraints
    return (f"enum_values={col.enum_values}\n"
            f"check_constraints kept for validator={len(chks)} "
            f"(0 => validator never re-checks enum membership independently)")


@probe("P15: VARCHAR max_length vs Faker output truncation + uniqueness")
def p15():
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      code VARCHAR(3) UNIQUE NOT NULL
    );
    """
    s = parse_ddl(ddl)
    try:
        g = run_engine(s, {"t": 50}, EngineConfig(seed=5))
        vals = [r["code"] for r in g["t"]]
        return f"generated {len(vals)} unique 3-char codes: {vals[:8]}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


@probe("P16: self-referential manager cycles across seeds (A manages B manages A)")
def p16():
    ddl = """
    CREATE TABLE employees (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      manager_id INT REFERENCES employees(id)
    );
    """
    def cycles_for(seed, n):
        g = run_engine(parse_ddl(ddl), {"employees": n}, EngineConfig(seed=seed))
        mgr = {r["id"]: r["manager_id"] for r in g["employees"]}
        found = set()
        for start in mgr:
            seen, cur = [], start
            while cur is not None and cur not in seen:
                seen.append(cur)
                cur = mgr.get(cur)
            if cur is not None:
                found.add(tuple(sorted(seen[seen.index(cur):])))
        return found
    lines, total = [], 0
    for seed in range(1, 11):
        c = cycles_for(seed, 200)
        total += len(c)
        ex = sorted(c, key=len)[0] if c else None
        lines.append(f"  seed {seed:>2}: {len(c)} cycle(s) example={ex}")
    return ("200 employees, seeds 1-10 (a real org chart is a tree):\n"
            + "\n".join(lines) + f"\nTOTAL distinct cycles: {total}")


@probe("P17: CSV NULL representation")
def p17():
    from datagen.dependency_graph import build_generation_plan
    from datagen.output import write_csv
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      note VARCHAR(50),
      qty INT
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 6}, EngineConfig(seed=4, null_rate=0.5))
    write_csv(s, g, build_generation_plan(s).order, "./_probe_out")
    raw = open("./_probe_out/t.csv", encoding="utf-8").read()
    return (raw[:200] + "\nNULL -> empty field. Postgres COPY reads that as empty-string "
            "for text cols (not NULL) and errors on INT cols unless NULL '' is set.")


@probe("P18: same seed, different calendar day (date.today() dependence)")
def p18():
    import datetime as _dt
    import datagen.generators.date_time as dtmod

    class FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2030, 1, 1)

    ddl = "CREATE TABLE t (id SERIAL PRIMARY KEY, d DATE NOT NULL);"
    run_a = run_engine(parse_ddl(ddl), {"t": 3}, EngineConfig(seed=5))
    dtmod.date = FakeDate  # simulate running the same command on a later day
    try:
        run_b = run_engine(parse_ddl(ddl), {"t": 3}, EngineConfig(seed=5))
    finally:
        dtmod.date = _dt.date
    a = [str(r["d"]) for r in run_a["t"]]
    b = [str(r["d"]) for r in run_b["t"]]
    return f"today:      {a}\nas-if 2030: {b}\nIDENTICAL = {a == b}"


@probe("P19: emitted SQL for INT columns (fallout of P7)")
def p19():
    from datagen.dependency_graph import build_generation_plan
    from datagen.output import write_sql
    ddl = """
    CREATE TABLE t (
      id SERIAL PRIMARY KEY,
      min_qty INT NOT NULL,
      max_qty INT NOT NULL,
      CONSTRAINT chk CHECK (max_qty > min_qty)
    );
    """
    s = parse_ddl(ddl)
    g = run_engine(s, {"t": 2}, EngineConfig(seed=5, null_rate=0.0))
    write_sql(s, g, build_generation_plan(s).order, "./_probe_out/insert.sql")
    return open("./_probe_out/insert.sql", encoding="utf-8").read()[:280]


if __name__ == "__main__":
    print("\n\n===== SUMMARY =====")
    for name, out in RESULTS:
        first = str(out).splitlines()[0] if str(out) else ""
        print(f"- {name}: {first}")
