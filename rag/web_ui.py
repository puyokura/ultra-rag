"""Memory browser served at /ui.

A single self-contained page. Text is rendered with ``textContent`` so memory
content cannot inject markup. The only write path is candidate adoption or
rejection; the server binds to a LAN address and the page has no authentication.
"""

from __future__ import annotations

import json
from typing import Any

from rag.service import RagService

KIND_LABELS = {
    "philosophy": "思想",
    "design_principle": "設計",
    "preference": "好み",
    "prohibition": "禁止",
    "requirement": "必須",
    "lesson": "教訓",
    "project_decision": "案件",
    "activity_summary": "活動",
}
STATUS_LABELS = {
    "adopted": "採用",
    "pending": "保留",
    "conflict": "競合",
    "superseded": "旧版",
    "forgotten": "忘却",
    "rejected": "却下",
    "deleted": "削除",
}

PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ultra-RAG 記憶台帳</title>
<style>
:root {
  --base: #0f1213; --surface: #161a1b; --surface-2: #1c2122; --rule: #2a3032;
  --ink: #e3e0d6; --muted: #939b9c; --accent: #6fa8c9;
  --ok: #7fae8b; --warn: #c9a96a; --bad: #c98383;
  --serif: "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", "Source Han Serif", serif;
  --sans: -apple-system, "Segoe UI", "Noto Sans JP", "Hiragino Sans", sans-serif;
  --mono: ui-monospace, "Cascadia Mono", "SFMono-Regular", monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--base); color: var(--ink); }
body { font: 13px/1.6 var(--sans); }
.page { max-width: 1180px; margin: 0 auto; padding: 18px 20px 48px; }
.masthead { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--rule); padding-bottom: 10px; }
h1 { font: 500 21px/1.2 var(--serif); letter-spacing: .01em; margin: 0; }
.ledger { color: var(--muted); font-size: 12px; }
.ledger b { color: var(--ink); font-weight: 500; font-family: var(--mono); font-size: 12px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 14px 0 4px; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field label { color: var(--muted); font-size: 11px; }
input[type="search"], select, button {
  background: var(--surface); color: var(--ink); border: 1px solid var(--rule);
  border-radius: 2px; padding: 5px 8px; font: inherit; min-height: 30px;
}
input[type="search"] { width: 280px; }
input[type="search"]:focus-visible, select:focus-visible, button:focus-visible, a:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 1px;
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
.search { flex: 1 1 260px; }
.tabbar { display: flex; gap: 18px; border-bottom: 1px solid var(--rule); margin-top: 16px; }
.tabbar button { background: none; border: 0; border-bottom: 1px solid transparent; border-radius: 0; color: var(--muted); padding: 7px 1px; }
.tabbar button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--accent); }
.count { color: var(--muted); font-size: 12px; margin: 10px 0 2px; }
.content { margin-top: 6px; }
.entry { display: grid; grid-template-columns: 224px minmax(0, 1fr); gap: 18px; padding: 11px 0; border-bottom: 1px solid var(--rule); }
.meta { color: var(--muted); font-size: 11.5px; overflow-wrap: anywhere; }
.meta .idline { display: flex; gap: 6px; align-items: center; }
.meta .id { font-family: var(--mono); font-size: 11px; color: var(--muted); }
.meta .state { color: var(--ink); }
.meta .kind { color: var(--accent); }
.meta .sep { margin: 0 5px; color: var(--rule); }
.body { min-width: 0; max-width: 74ch; }
.statement { white-space: pre-wrap; overflow-wrap: anywhere; }
.detail { color: var(--muted); font-size: 12px; margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
.detail em { color: var(--ink); font-style: normal; }
.copy { padding: 0 6px; min-height: 21px; font-size: 10.5px; color: var(--muted); }
.actions { margin-top: 8px; display: flex; gap: 6px; }
.actions button { min-height: 26px; font-size: 12px; padding: 2px 10px; }
.activity { grid-template-columns: 150px minmax(0, 1fr); }
.activity .date { font: 500 14px/1.4 var(--serif); color: var(--ink); }
.activity h3 { margin: 0 0 4px; font: 500 15px/1.45 var(--serif); }
.activity ul { margin: 4px 0 0; padding-left: 18px; }
.activity li { margin: 1px 0; }
.activity .label { color: var(--muted); font-size: 11.5px; }
.prose { max-width: 74ch; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface); border: 1px solid var(--rule); border-radius: 2px; padding: 16px 18px; }
.prose h1 { font: 500 18px/1.4 var(--serif); margin: 18px 0 8px; }
.prose h2 { font: 500 15px/1.4 var(--serif); margin: 16px 0 6px; }
.prose h3 { font: 500 13px/1.4 var(--sans); margin: 12px 0 4px; color: var(--accent); }
.empty { color: var(--muted); padding: 18px 0; max-width: 60ch; }
.toast { position: fixed; right: 18px; bottom: 18px; background: var(--surface-2); border: 1px solid var(--rule); padding: 7px 11px; border-radius: 2px; opacity: 0; transition: opacity .16s; pointer-events: none; }
.toast.show { opacity: 1; }
footer { margin-top: 26px; color: var(--muted); font-size: 11.5px; border-top: 1px solid var(--rule); padding-top: 10px; max-width: 80ch; }
@media (max-width: 820px) {
  .entry, .activity { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .masthead { flex-direction: column; gap: 4px; }
  .toolbar input[type="search"] { width: 100%; }
  .page { padding: 14px 14px 40px; }
}
@media (prefers-reduced-motion: reduce) { .toast { transition: none; } }
</style>
</head>
<body>
<div class="page">
<header class="masthead">
  <h1>記憶台帳</h1>
  <div class="ledger" id="ledger">読み込み中</div>
</header>

<div class="toolbar">
  <div class="field search">
    <label for="q">検索</label>
    <input type="search" id="q" placeholder="本文・条件・ID" autocomplete="off">
  </div>
  <div class="field"><label for="status">状態</label><select id="status"><option value="">すべて</option></select></div>
  <div class="field"><label for="kind">種別</label><select id="kind"><option value="">すべて</option></select></div>
  <div class="field"><label for="scope">範囲</label><select id="scope"><option value="">すべて</option></select></div>
  <div class="field"><label>&nbsp;</label><button id="reload" type="button">再読み込み</button></div>
</div>

<div class="tabbar" role="tablist">
  <button type="button" role="tab" data-tab="memories" aria-selected="true">記憶</button>
  <button type="button" role="tab" data-tab="activities" aria-selected="false">活動記録</button>
  <button type="button" role="tab" data-tab="candidates" aria-selected="false">候補</button>
  <button type="button" role="tab" data-tab="profile" aria-selected="false">プロフィール</button>
  <button type="button" role="tab" data-tab="constraints" aria-selected="false">禁止・必須</button>
</div>

<p class="count" id="count"></p>
<main class="content" id="panel"></main>
<footer>閲覧は読み取り専用。候補の承認・却下だけを書き込める。この画面はLAN内の端末から認証なしで開けるため、外部公開しない。本文は省略せずに表示する。</footer>
</div>
<div class="toast" id="toast"></div>
<script>
const KIND_LABELS = __KIND_LABELS__;
const STATUS_LABELS = __STATUS_LABELS__;
const state = { data: null, tab: "memories", q: "", status: "", kind: "", scope: "" };
const el = (id) => document.getElementById(id);
const label = (map, key) => map[key] || key;

function toast(text) {
  const node = el("toast");
  node.textContent = text;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 1300);
}

function copyText(value) {
  const done = () => toast("IDをコピーしました");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done, () => fallbackCopy(value, done));
  } else {
    fallbackCopy(value, done);
  }
}

function fallbackCopy(value, done) {
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); done(); } catch (error) { toast("コピーできません"); }
  document.body.removeChild(area);
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function metaLine(record) {
  const meta = make("div", "meta");
  const idline = make("div", "idline");
  const copy = make("button", "copy", "ID");
  copy.type = "button";
  copy.title = "記憶IDをコピー";
  copy.addEventListener("click", () => copyText(record.id));
  idline.appendChild(copy);
  idline.appendChild(make("span", "id", record.id));
  meta.appendChild(idline);

  const facts = make("div");
  facts.appendChild(make("span", "state", label(STATUS_LABELS, record.status)));
  facts.appendChild(make("span", "sep", "/"));
  facts.appendChild(make("span", "kind", label(KIND_LABELS, record.kind)));
  facts.appendChild(make("span", "sep", "/"));
  facts.appendChild(make("span", null, record.scope));
  if (record.project_id) {
    facts.appendChild(make("span", "sep", "/"));
    facts.appendChild(make("span", null, record.project_id));
  }
  meta.appendChild(facts);
  meta.appendChild(make("div", null, "rev " + record.revision + "  " + (record.recorded_at || "")));
  if (record.owner_session_id) meta.appendChild(make("div", null, "session " + record.owner_session_id));
  return meta;
}

function entry(record) {
  const row = make("article", "entry");
  row.id = record.id;
  row.appendChild(metaLine(record));
  const body = make("div", "body");
  body.appendChild(make("div", "statement", record.statement));
  const conditions = record.conditions || [];
  const exceptions = record.exceptions || [];
  if (conditions.length || exceptions.length) {
    const detail = make("div", "detail");
    if (conditions.length) {
      detail.appendChild(make("em", null, "適用条件 "));
      detail.appendChild(document.createTextNode(conditions.join(" / ")));
    }
    if (conditions.length && exceptions.length) detail.appendChild(document.createElement("br"));
    if (exceptions.length) {
      detail.appendChild(make("em", null, "例外 "));
      detail.appendChild(document.createTextNode(exceptions.join(" / ")));
    }
    body.appendChild(detail);
  }
  return row;
}

function activityEntry(record) {
  const row = make("article", "entry activity");
  row.id = record.id;
  const activity = record.activity || {};
  const side = make("div");
  side.appendChild(make("div", "date", activity.date || ""));
  const meta = make("div", "meta");
  meta.appendChild(make("div", "id", record.id));
  meta.appendChild(make("div", null, label(STATUS_LABELS, record.status) + " / " + record.scope));
  side.appendChild(meta);
  row.appendChild(side);

  const body = make("div", "body");
  body.appendChild(make("h3", null, activity.overview || record.statement));
  for (const [key, title] of [["achievements", "達成"], ["decisions", "決定"], ["remaining", "残件"]]) {
    const values = activity[key] || [];
    if (!values.length) continue;
    body.appendChild(make("div", "label", title));
    const list = make("ul");
    for (const value of values) list.appendChild(make("li", null, value));
    body.appendChild(list);
  }
  body.appendChild(make("div", "detail", "rev " + record.revision + "  " + (record.recorded_at || "")));
  row.appendChild(body);
  return row;
}

function candidateEntry(record) {
  const row = entry(record);
  const actions = make("div", "actions");
  const adopt = make("button", "adopt", "承認");
  adopt.type = "button";
  adopt.addEventListener("click", () => decide(record.id, "adopt"));
  const reject = make("button", "reject", "却下");
  reject.type = "button";
  reject.addEventListener("click", () => decide(record.id, "reject"));
  actions.appendChild(adopt);
  actions.appendChild(reject);
  row.querySelector(".body").appendChild(actions);
  return row;
}

async function decide(memoryId, decision) {
  const verb = decision === "adopt" ? "承認" : "却下";
  if (!window.confirm(verb + "します。 " + memoryId)) return;
  try {
    const response = await fetch("/ui/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ultra-RAG-UI": "1" },
      body: JSON.stringify({ candidate_id: memoryId, decision: decision }),
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    toast(verb + "しました");
    await load();
  } catch (error) {
    toast(verb + "できません（" + error.message + "）");
  }
}

function matches(record) {
  if (state.status && record.status !== state.status) return false;
  if (state.kind && record.kind !== state.kind) return false;
  if (state.scope && record.scope !== state.scope) return false;
  if (!state.q) return true;
  const needle = state.q.toLowerCase();
  const fields = [record.id, record.statement, ...(record.conditions || []), ...(record.exceptions || [])];
  if (record.activity) {
    fields.push(record.activity.overview || "");
    for (const key of ["achievements", "decisions", "remaining"]) fields.push(...(record.activity[key] || []));
  }
  return fields.some((value) => String(value).toLowerCase().includes(needle));
}

function render() {
  const data = state.data;
  if (!data) return;
  const panel = el("panel");
  panel.replaceChildren();
  if (state.tab === "profile" || state.tab === "constraints") {
    panel.appendChild(make("div", "prose", state.tab === "profile" ? data.profile : data.constraints));
    el("count").textContent = "";
    return;
  }
  const source = state.tab === "activities"
    ? data.activities
    : state.tab === "candidates" ? data.candidates : data.memories;
  const records = source.filter(matches);
  el("count").textContent = records.length + "件を表示 / 全" + source.length + "件";
  if (!records.length) {
    panel.appendChild(make("p", "empty", "条件に合う記録はありません。検索語を短くするか、状態と種別の絞り込みを外してください。"));
    return;
  }
  for (const record of records) {
    if (state.tab === "activities") panel.appendChild(activityEntry(record));
    else if (state.tab === "candidates") panel.appendChild(candidateEntry(record));
    else panel.appendChild(entry(record));
  }
}

function fillSelect(select, values, labels) {
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labels ? label(labels, value) : value;
    select.appendChild(option);
  }
}

async function load() {
  try {
    const response = await fetch("/ui/data", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    state.data = data;
    const ledger = el("ledger");
    ledger.replaceChildren();
    for (const [key, value] of [["改訂", data.revision], ["記憶", data.counts.memories], ["活動", data.counts.activities], ["候補", data.counts.candidates]]) {
      ledger.appendChild(document.createTextNode(key + " "));
      ledger.appendChild(make("b", null, value));
      ledger.appendChild(document.createTextNode("  "));
    }
    const statuses = [...new Set(data.memories.map((record) => record.status))].sort();
    const kinds = [...new Set(data.memories.map((record) => record.kind))].sort();
    const scopes = [...new Set(data.memories.map((record) => record.scope))].sort();
    const statusSelect = el("status"), kindSelect = el("kind"), scopeSelect = el("scope");
    for (const select of [statusSelect, kindSelect, scopeSelect]) select.length = 1;
    fillSelect(statusSelect, statuses, STATUS_LABELS);
    fillSelect(kindSelect, kinds, KIND_LABELS);
    fillSelect(scopeSelect, scopes);
    statusSelect.value = state.status;
    kindSelect.value = state.kind;
    scopeSelect.value = state.scope;
    render();
    if (location.hash) {
      const target = document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView({ block: "center" });
    }
  } catch (error) {
    el("ledger").textContent = "";
    const panel = el("panel");
    panel.replaceChildren();
    panel.appendChild(make("p", "empty", "記録を読み込めませんでした（" + error.message + "）。再読み込みしてください。"));
  }
}

for (const tab of document.querySelectorAll(".tabbar button")) {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    for (const other of document.querySelectorAll(".tabbar button")) {
      other.setAttribute("aria-selected", String(other === tab));
    }
    render();
  });
}
el("q").addEventListener("input", (event) => { state.q = event.target.value.trim(); render(); });
el("status").addEventListener("change", (event) => { state.status = event.target.value; render(); });
el("kind").addEventListener("change", (event) => { state.kind = event.target.value; render(); });
el("scope").addEventListener("change", (event) => { state.scope = event.target.value; render(); });
el("reload").addEventListener("click", load);
load();
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__KIND_LABELS__", json.dumps(KIND_LABELS, ensure_ascii=False))
PAGE = PAGE.replace("__STATUS_LABELS__", json.dumps(STATUS_LABELS, ensure_ascii=False))


def build_view(service: RagService) -> dict[str, Any]:
    """Return the payload rendered by ``/ui``.

    Only memory text and metadata are exposed; no key names, values, request
    bodies, or password state can reach this view.
    """
    snapshot = service.snapshot()
    state = service.store.load()
    memories = [record.to_dict() for record in state.memories.values() if record.status != "deleted"]
    memories.sort(key=lambda item: (item.get("status", ""), item.get("recorded_at", ""), item["id"]), reverse=True)
    candidates = [item for item in memories if item.get("status") in ("pending", "conflict")]
    activities = [item for item in memories if item.get("kind") == "activity_summary"]
    activities.sort(
        key=lambda item: ((item.get("activity") or {}).get("date") or "", item.get("recorded_at", ""), item["id"]),
        reverse=True,
    )
    return {
        "revision": snapshot["revision"],
        "generated_at": _now(),
        "profile": snapshot["profile"],
        "constraints": snapshot["constraints"],
        "memories": memories,
        "candidates": candidates,
        "activities": activities,
        "counts": {
            "memories": len(memories),
            "candidates": len(candidates),
            "activities": len(activities),
            "sessions": len(state.sessions),
        },
    }


def _now() -> str:
    from contracts.util import utc_now

    return utc_now()
