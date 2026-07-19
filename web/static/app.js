/* Synth-Scale playground — vanilla JS, no dependencies. */
"use strict";

const EXAMPLE_DDL = `-- Example: a small shop. Edit freely — FKs, CHECKs and enums all work.
CREATE TABLE categories (
  id SERIAL PRIMARY KEY,
  name VARCHAR(60) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  email VARCHAR(120) NOT NULL UNIQUE,
  first_name VARCHAR(40) NOT NULL,
  city VARCHAR(60),
  country VARCHAR(60),
  is_active BOOLEAN NOT NULL,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  product_name VARCHAR(80) NOT NULL,
  price NUMERIC(8,2) NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL CHECK (stock >= 0),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  status VARCHAR(12) NOT NULL
    CHECK (status IN ('pending','paid','shipped','delivered','cancelled')),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  placed_at TIMESTAMP NOT NULL,
  shipped_at TIMESTAMP,
  delivered_at TIMESTAMP,
  cancelled_at TIMESTAMP
);
`;

const $ = (sel) => document.querySelector(sel);
const ddlEl = $("#ddl");
const rowsEl = $("#rows");
const rowsOut = $("#rows-out");
const seedEl = $("#seed");
const genBtn = $("#generate");
const sqlBtn = $("#dl-sql");
const csvBtn = $("#dl-csv");
const errEl = $("#error");
const verdictEl = $("#verdict");
const resultsEl = $("#results");

ddlEl.value = EXAMPLE_DDL;
rowsEl.addEventListener("input", () => (rowsOut.textContent = rowsEl.value));

function currentBody(format) {
  return {
    ddl: ddlEl.value,
    rows: parseInt(rowsEl.value, 10) || 50,
    seed: parseInt(seedEl.value, 10) || 0,
    format,
  };
}

function showError(msg) {
  errEl.textContent = msg;
  errEl.hidden = false;
  verdictEl.hidden = true;
}

function clearError() {
  errEl.hidden = true;
  errEl.textContent = "";
}

async function apiGenerate(format) {
  const resp = await fetch("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentBody(format)),
  });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try {
      const data = await resp.json();
      if (data.detail) {
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      }
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return resp;
}

function renderPreview(data) {
  resultsEl.textContent = "";
  const v = data.validation;
  const ok = v.violations_found === 0;
  verdictEl.textContent = ok
    ? `✓ 0 violations — ${v.constraints_checked} constraints checked — ` +
      `${data.total_rows} rows — deterministic seed ${data.seed}`
    : `✗ ${v.violations_found} violations found — see server logs`;
  verdictEl.classList.toggle("bad", !ok);
  verdictEl.hidden = false;

  for (const t of data.tables) {
    const block = document.createElement("div");
    block.className = "table-block";

    const h = document.createElement("h3");
    h.textContent = t.name + " ";
    const span = document.createElement("span");
    span.className = "rowcount";
    span.textContent = `— showing ${t.rows.length} of ${t.total_rows} rows`;
    h.appendChild(span);
    block.appendChild(h);

    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const table = document.createElement("table");
    table.className = "data";

    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const c of t.columns) {
      const th = document.createElement("th");
      th.textContent = c;
      hr.appendChild(th);
    }
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const row of t.rows) {
      const tr = document.createElement("tr");
      for (const cell of row) {
        const td = document.createElement("td");
        if (cell === null) {
          td.textContent = "NULL";
          td.className = "null";
        } else {
          td.textContent = String(cell);
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    scroll.appendChild(table);
    block.appendChild(scroll);
    resultsEl.appendChild(block);
  }
}

function setBusy(busy) {
  genBtn.disabled = busy;
  genBtn.textContent = busy ? "Generating…" : "Generate";
}

genBtn.addEventListener("click", async () => {
  clearError();
  setBusy(true);
  try {
    const resp = await apiGenerate("preview");
    renderPreview(await resp.json());
    sqlBtn.disabled = false;
    csvBtn.disabled = false;
  } catch (e) {
    showError(e.message);
    resultsEl.textContent = "";
  } finally {
    setBusy(false);
  }
});

async function download(format, btn) {
  clearError();
  btn.disabled = true;
  try {
    const resp = await apiGenerate(format);
    const blob = await resp.blob();
    const dispo = resp.headers.get("Content-Disposition") || "";
    const m = dispo.match(/filename="([^"]+)"/);
    const name = m ? m[1] : format === "sql" ? "synth_scale.sql" : "synth_scale_csv.zip";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
  }
}

sqlBtn.addEventListener("click", () => download("sql", sqlBtn));
csvBtn.addEventListener("click", () => download("csv", csvBtn));

/* ---- pip chip copy ---- */
const chip = $("#pip-chip");
chip.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText("pip install synth-scale");
    chip.classList.add("copied");
    chip.querySelector(".copy-hint").textContent = "copied";
    setTimeout(() => {
      chip.classList.remove("copied");
      chip.querySelector(".copy-hint").textContent = "copy";
    }, 1500);
  } catch (_) { /* clipboard unavailable; the text is selectable */ }
});

/* ---- waitlist forms (top + bottom share this) ---- */
for (const form of document.querySelectorAll("[data-waitlist]")) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = form.querySelector('input[type="email"]');
    const msg = form.querySelector(".waitlist-msg");
    msg.classList.remove("err");
    msg.textContent = "…";
    try {
      const resp = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: input.value }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Something went wrong.");
      msg.textContent = data.added
        ? `You're in — #${data.count} on the list.`
        : "You're already on the list — see you at launch.";
      input.value = "";
      updateCount(data.count);
    } catch (e) {
      msg.classList.add("err");
      msg.textContent = e.message;
    }
  });
}

function updateCount(n) {
  const el = document.querySelector("[data-waitlist-count]");
  if (el && n >= 5) {
    el.textContent = `${n} developers on the waitlist`;
    el.hidden = false;
  }
}

fetch("/api/waitlist/count")
  .then((r) => r.json())
  .then((d) => updateCount(d.count))
  .catch(() => {});
