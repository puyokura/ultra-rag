"""Read-only memory browser served at /ui.

The page is a single self-contained HTML document with inline CSS and script.
It renders text with ``textContent`` so memory statements cannot inject markup.
It exposes no write path and no key metadata; any device on the same LAN can
read it, so the server binds to a LAN address only.
"""

from __future__ import annotations

from typing import Any

from rag.service import RagService

PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ultra-RAG memory</title>
<style>
:root {
  --bg: #101317; --panel: #171b21; --line: #262c35; --text: #d8dee7;
  --muted: #8d98a7; --accent: #4f9cf9; --ok: #47b881; --warn: #d9a441; --bad: #d96a6a;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
body { font: 13px/1.45 -apple-system, "Segoe UI", "Noto Sans JP", sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 10px 12px 40px; }
header { position: sticky; top: 0; z-index: 5; background: var(--bg); border-bottom: 1px solid var(--line); padding: 8px 0; }
h1 { font-size: 15px; margin: 0 0 6px; font-weight: 600; letter-spacing: .02em; }
.meta { color: var(--muted); font-size: 12px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
input[type="search"], select, button {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 4px; padding: 4px 8px; font: inherit; min-height: 28px;
}
input[type="search"] { flex: 1 1 220px; }
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
.tabs { display: flex; gap: 4px; margin: 10px 0 8px; flex-wrap: wrap; }
.tabs button { border-bottom: 2px solid transparent; border-radius: 0; background: none; }
.tabs button[aria-selected="true"] { border-bottom-color: var(--accent); color: #fff; }
.count { color: var(--muted); font-size: 12px; margin: 0 0 6px; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 720px; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 12px; white-space: nowrap; }
tr.row { min-height: 28px; }
tr.row:hover { background: #1c222a; }
tr.row:target { outline: 1px solid var(--accent); }
.id { font-family: ui-monospace, "Cascadia Mono", monospace; font-size: 11.5px; color: var(--muted); white-space: nowrap; }
.statement { white-space: pre-wrap; word-break: break-word; }
.sub { color: var(--muted); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
.tag { display: inline-block; padding: 0 6px; border: 1px solid var(--line); border-radius: 3px; font-size: 11px; margin-right: 4px; }
.adopted { color: var(--ok); border-color: var(--ok); }
.pending, .conflict { color: var(--warn); border-color: var(--warn); }
.rejected, .forgotten { color: var(--bad); border-color: var(--bad); }
.prose { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; white-space: pre-wrap; word-break: break-word; }
.copy { padding: 0 6px; min-height: 22px; font-size: 11px; }
.empty { color: var(--muted); padding: 12px 0; }
.toast { position: fixed; right: 12px; bottom: 12px; background: #20262f; border: 1px solid var(--line); padding: 6px 10px; border-radius: 4px; opacity: 0; transition: opacity .15s; pointer-events: none; }
.toast.show { opacity: 1; }
footer { margin-top: 18px; color: var(--muted); font-size: 11.5px; border-top: 1px solid var(--line); padding-top: 8px; }
a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>ultra-RAG memory</h1>
  <div class="meta" id="meta">読み込み中</div>
  <div class="toolbar">
    <input type="search" id="q" placeholder="検索（文・条件・ID）" autocomplete="off">
    <select id="status"><option value="">状態: すべて</option></select>
    <select id="scope"><option value="">範囲: すべて</option></select>
    <button id="reload" type="button">再読み込み</button>
  </div>
</header>
<div class="tabs" role="tablist">
  <button type="button" role="tab" data-tab="memories" aria-selected="true">記憶</button>
  <button type="button" role="tab" data-tab="candidates" aria-selected="false">候補</button>
  <button type="button" role="tab" data-tab="profile" aria-selected="false">プロフィール</button>
  <button type="button" role="tab" data-tab="constraints" aria-selected="false">禁止・必須</button>
</div>
<p class="count" id="count"></p>
<div id="panel"></div>
<footer>閲覧は読み取り専用。候補の承認・却下だけは書き込みできる（LAN内・認証なし）。外部公開しない。表示はすべて原文で、省略しない。</footer>
</div>
<div class="toast" id="toast"></div>
<script>
const state = { data: null, tab: "memories", q: "", status: "", scope: "" };
const el = (id) => document.getElementById(id);
const toast = (text) => {
  const node = el("toast");
  node.textContent = text;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 1200);
};
function copyText(value) {
  const done = () => toast("コピーしました");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done, () => fallback(value, done));
  } else {
    fallback(value, done);
  }
}
function fallback(value, done) {
  const area = document.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("コピーできません"); }
  document.body.removeChild(area);
}
function cell(text, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text == null ? "" : String(text);
  return td;
}
function tag(text, kind) {
  const span = document.createElement("span");
  span.className = "tag " + (kind || "");
  span.textContent = text;
  return span;
}
function matches(record) {
  if (state.status && record.status !== state.status) return false;
  if (state.scope && record.scope !== state.scope) return false;
  if (!state.q) return true;
  const needle = state.q.toLowerCase();
  const fields = [record.id, record.statement, ...(record.conditions || []), ...(record.exceptions || []), record.kind, record.scope, record.project_id || ""];
  return fields.some((value) => String(value).toLowerCase().includes(needle));
}
function renderTable(records, columns, rowBuilder) {
  const panel = el("panel");
  panel.textContent = "";
  if (!records.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "該当する記憶はありません。";
    panel.appendChild(empty);
    return;
  }
  const scroll = document.createElement("div");
  scroll.className = "scroll";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.appendChild(th);
  }
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  for (const record of records) body.appendChild(rowBuilder(record));
  table.appendChild(body);
  scroll.appendChild(table);
  panel.appendChild(scroll);
}
function memoryRow(record) {
  const tr = document.createElement("tr");
  tr.className = "row";
  tr.id = record.id;
  const idCell = document.createElement("td");
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "copy";
  copy.textContent = "ID";
  copy.title = "記憶IDをコピー";
  copy.addEventListener("click", () => copyText(record.id));
  const code = document.createElement("span");
  code.className = "id";
  code.textContent = record.id;
  idCell.appendChild(copy);
  idCell.appendChild(document.createTextNode(" "));
  idCell.appendChild(code);
  tr.appendChild(idCell);
  const statusCell = document.createElement("td");
  statusCell.appendChild(tag(record.status, record.status));
  statusCell.appendChild(tag(record.kind));
  statusCell.appendChild(tag(record.scope));
  if (record.project_id) statusCell.appendChild(tag(record.project_id));
  tr.appendChild(statusCell);
  const statement = document.createElement("td");
  statement.className = "statement";
  statement.textContent = record.statement;
  if ((record.conditions || []).length || (record.exceptions || []).length) {
    const sub = document.createElement("div");
    sub.className = "sub";
    if ((record.conditions || []).length) sub.textContent += "適用条件: " + record.conditions.join(" / ");
    if ((record.exceptions || []).length) sub.textContent += (sub.textContent ? "\\n" : "") + "例外: " + record.exceptions.join(" / ");
    statement.appendChild(sub);
  }
  tr.appendChild(statement);
  tr.appendChild(cell("rev " + record.revision + "\\n" + (record.recorded_at || ""), "sub"));
  return tr;
}
async function decide(memoryId, decision) {
  const label = decision === "adopt" ? "承認" : "却下";
  if (!window.confirm(label + "します。\n" + memoryId)) return;
  try {
    const response = await fetch("/ui/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ultra-RAG-UI": "1" },
      body: JSON.stringify({ candidate_id: memoryId, decision: decision }),
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    toast(label + "しました");
    await load();
  } catch (error) {
    toast(label + "できません（" + error.message + "）");
  }
}
function candidateRow(record) {
  const tr = memoryRow(record);
  const actions = document.createElement("td");
  for (const [label, decision] of [["承認", "adopt"], ["却下", "reject"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = label;
    button.addEventListener("click", () => decide(record.id, decision));
    actions.appendChild(button);
    actions.appendChild(document.createTextNode(" "));
  }
  tr.appendChild(actions);
  return tr;
}
function render() {
  const data = state.data;
  if (!data) return;
  if (state.tab === "memories" || state.tab === "candidates") {
    const source = state.tab === "candidates" ? data.candidates : data.memories;
    const records = source.filter(matches);
    el("count").textContent = records.length + " 件を表示 / " + source.length + " 件";
    const columns = state.tab === "candidates"
      ? ["ID", "状態・種別・範囲", "内容", "改訂・記録日", "操作"]
      : ["ID", "状態・種別・範囲", "内容", "改訂・記録日"];
    renderTable(records, columns, state.tab === "candidates" ? candidateRow : memoryRow);
    return;
  }
  const panel = el("panel");
  panel.textContent = "";
  const box = document.createElement("div");
  box.className = "prose";
  box.textContent = state.tab === "profile" ? data.profile : data.constraints;
  panel.appendChild(box);
  el("count").textContent = "";
}
function fillSelect(select, label, values) {
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label + ": " + value;
    select.appendChild(option);
  }
}
async function load() {
  try {
    const response = await fetch("/ui/data", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    state.data = data;
    el("meta").textContent = "revision " + data.revision + " / 記憶 " + data.counts.memories + " / 候補 " + data.counts.candidates + " / 生成 " + data.generated_at;
    const statuses = [...new Set(data.memories.map((m) => m.status))].sort();
    const scopes = [...new Set(data.memories.map((m) => m.scope))].sort();
    const statusSelect = el("status");
    const scopeSelect = el("scope");
    statusSelect.length = 1;
    scopeSelect.length = 1;
    fillSelect(statusSelect, "状態", statuses);
    fillSelect(scopeSelect, "範囲", scopes);
    statusSelect.value = state.status;
    scopeSelect.value = state.scope;
    render();
    if (location.hash) {
      const target = document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView({ block: "center" });
    }
  } catch (error) {
    el("meta").textContent = "読み込みに失敗しました（" + error.message + "）";
  }
}
for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => {
    state.tab = tab.dataset.tab;
    for (const other of document.querySelectorAll(".tabs button")) other.setAttribute("aria-selected", String(other === tab));
    render();
  });
}
el("q").addEventListener("input", (event) => { state.q = event.target.value.trim(); render(); });
el("status").addEventListener("change", (event) => { state.status = event.target.value; render(); });
el("scope").addEventListener("change", (event) => { state.scope = event.target.value; render(); });
el("reload").addEventListener("click", load);
load();
</script>
</body>
</html>
"""


def build_view(service: RagService) -> dict[str, Any]:
    """Return the read-only payload rendered by ``/ui``.

    Only memory text and metadata are exposed: no key names, values, request
    bodies, or password state can reach this view.
    """
    snapshot = service.snapshot()
    state = service.store.load()
    memories = [record.to_dict() for record in state.memories.values() if record.status != "deleted"]
    memories.sort(key=lambda item: (item.get("status", ""), item.get("recorded_at", ""), item["id"]), reverse=True)
    candidates = [item for item in memories if item.get("status") in ("pending", "conflict")]
    return {
        "revision": snapshot["revision"],
        "generated_at": snapshot.get("generated_at") or _now(),
        "profile": snapshot["profile"],
        "constraints": snapshot["constraints"],
        "memories": memories,
        "candidates": candidates,
        "counts": {
            "memories": len(memories),
            "candidates": len(candidates),
            "sessions": len(state.sessions),
        },
    }


def _now() -> str:
    from contracts.util import utc_now

    return utc_now()
