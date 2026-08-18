/* Private stats dashboard. Not linked from the site; not indexed. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const KEY_STORAGE = "ss_stats_key";

const keyInput = $("#stats-key");
const loadBtn = $("#stats-load");
const forgetBtn = $("#stats-forget");
const errEl = $("#stats-error");
const bodyEl = $("#stats-body");

const stored = localStorage.getItem(KEY_STORAGE);
if (stored) keyInput.value = stored;

function showError(msg) {
  errEl.textContent = msg;
  errEl.hidden = false;
  bodyEl.hidden = true;
}

function fillTable(tableId, rows) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.textContent = "";
  const entries = Object.entries(rows);
  if (!entries.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.textContent = "No data yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const [name, count] of entries) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = name;
    const tdCount = document.createElement("td");
    tdCount.textContent = String(count);
    tr.appendChild(tdName);
    tr.appendChild(tdCount);
    tbody.appendChild(tr);
  }
}

function fillFeedbackList(responses) {
  const list = $("#feedback-list");
  const countEl = $("#feedback-count");
  list.textContent = "";
  countEl.textContent = responses.length ? `(${responses.length} shown)` : "";

  if (!responses.length) {
    const p = document.createElement("p");
    p.className = "section-sub";
    p.textContent = "No responses yet.";
    list.appendChild(p);
    return;
  }

  for (const r of responses) {
    const card = document.createElement("div");
    card.className = "feedback-response";

    const head = document.createElement("div");
    head.className = "feedback-response-head";
    const pill = document.createElement("span");
    pill.className = `would-use-pill ${r.would_use || ""}`;
    pill.textContent = r.would_use || "unknown";
    head.appendChild(pill);
    const ts = document.createElement("span");
    ts.textContent = r.ts ? new Date(r.ts).toLocaleString() : "";
    head.appendChild(ts);
    if (r.email) {
      const email = document.createElement("span");
      email.textContent = r.email;
      head.appendChild(email);
    }
    card.appendChild(head);

    const dl = document.createElement("dl");
    if (r.use_case) {
      const dt = document.createElement("dt");
      dt.textContent = "What for";
      const dd = document.createElement("dd");
      dd.textContent = r.use_case;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    if (r.blockers) {
      const dt = document.createElement("dt");
      dt.textContent = "What would stop them";
      const dd = document.createElement("dd");
      dd.textContent = r.blockers;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    if (dl.children.length) card.appendChild(dl);

    list.appendChild(card);
  }
}

async function load() {
  errEl.hidden = true;
  const key = keyInput.value.trim();
  if (!key) {
    showError("Enter the stats key first.");
    return;
  }
  loadBtn.disabled = true;
  loadBtn.textContent = "Loading...";
  try {
    const resp = await fetch("/api/stats", { headers: { "X-Stats-Key": key } });
    if (!resp.ok) {
      throw new Error(resp.status === 404 ? "Wrong key, or stats aren't enabled on this deploy." : `Request failed (${resp.status})`);
    }
    const data = await resp.json();
    localStorage.setItem(KEY_STORAGE, key);

    $("#stat-pageviews").textContent = data.pageviews;
    $("#stat-sessions").textContent = data.unique_sessions;
    $("#stat-waitlist").textContent = data.waitlist_count;
    $("#stat-contact").textContent = data.contact_messages;
    fillTable("table-clicks", data.clicks_by_target || {});
    fillTable("table-paths", data.pageviews_by_path || {});

    const survey = data.survey || { total: 0, would_use: {}, use_case_counts: {} };
    $("#stat-survey-total").textContent = survey.total || 0;
    $("#stat-survey-yes").textContent = survey.would_use?.yes || 0;
    $("#stat-survey-maybe").textContent = survey.would_use?.maybe || 0;
    $("#stat-survey-no").textContent = survey.would_use?.no || 0;
    fillTable("table-use-cases", survey.use_case_counts || {});
    fillFeedbackList(survey.responses || []);

    bodyEl.hidden = false;
  } catch (e) {
    showError(e.message);
  } finally {
    loadBtn.disabled = false;
    loadBtn.textContent = "Load";
  }
}

loadBtn.addEventListener("click", load);
keyInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") load();
});
forgetBtn.addEventListener("click", () => {
  localStorage.removeItem(KEY_STORAGE);
  keyInput.value = "";
  bodyEl.hidden = true;
  errEl.hidden = true;
});

if (stored) load();
