// The side panel's HTML. Every value from the server is agent-written and
// untrusted, so each passes through `esc`, `fmt` (numbers only) or the
// escaping markdown renderer. No DOM, so it is tested under node.

import { fmt } from "./format.js";
import { esc, markdown } from "./md.js";

const KIND_UP = { parent: "parent", derived: "derived from", supersedes: "supersedes", related: "related", cites: "cites" };
const KIND_DOWN = { parent: "child", derived: "derived by", supersedes: "superseded by", related: "related", cites: "cited by" };

// `fill(status)` gives the status colour.
export function panelHTML(doc, fill) {
  const e = doc.entry;
  const field = (label, value) => value ? `<h3>${label}</h3><p>${esc(String(value))}</p>` : "";
  const r = e.result;
  let html = `<p class="panel-id"><span class="pill" style="background:${esc(fill(e.status))}">${esc(e.status)}</span> ${esc(e.id)}, ${esc(e.track)}</p>
    <h2>${esc(e.title)}</h2>`;
  if (e.claim) {
    const over = e.claim.overrun;
    html += `<p class="claim${over ? " over" : ""}">Claimed by <strong>${esc(e.claim.session)}</strong> since ${esc(String(e.claim.at ?? "").slice(0, 16).replace("T", " "))}` +
      (over ? `, past its ${esc(over.meter)} ceiling (${fmt(over.spent)} of ${fmt(over.cap)})` : "") +
      (e.claim.why ? `<br><span class="muted">${esc(e.claim.why)}</span>` : "") + "</p>";
  }
  html += field("Hypothesis", e.hypothesis) + field("Prediction", e.prediction);
  if (r) {
    html += `<h3>Verdict</h3><p><strong>${esc(r.verdict)}</strong>` +
      (r.closure_kind ? ` (${esc(r.closure_kind)})` : "") +
      ` by ${esc(r.session)}, ${esc(String(r.at ?? "").slice(0, 10))}</p>` +
      (r.summary ? `<p>${esc(r.summary)}</p>` : "") +
      (r.reopen_condition ? `<p class="muted">Reopens if: ${esc(r.reopen_condition)}</p>` : "");
  }
  if (e.gates && e.gates.length) {
    html += `<h3>Gates</h3><ul>${e.gates.map((g) => `<li>${esc(g.name)}: ${esc(g.state || "")}${g.required ? " (required)" : ""}</li>`).join("")}</ul>`;
  }
  if (doc.upstream.length || doc.downstream.length) {
    const item = (l, kinds) => `<li>${esc(kinds[l.kind])} <a href="#graph" data-entry="${esc(l.id)}">${esc(l.id)}</a> ${esc(l.title)}</li>`;
    html += `<h3>Links</h3><ul class="links">${doc.upstream.map((l) => item(l, KIND_UP)).join("")}${doc.downstream.map((l) => item(l, KIND_DOWN)).join("")}</ul>
      <p><button type="button" data-focus="${esc(e.id)}">Show only this lineage</button></p>`;
  }
  html += runsHTML(doc.runs);
  if (doc.memo) {
    const m = doc.memo;
    html += `<h3>Memo <span class="muted">${esc(m.path)}</span></h3>`;
    if (!m.exists) html += `<p class="bad">The memo file is missing.</p>`;
    else if (m.text === null) html += `<p class="muted">Too large to show here (${fmt(m.size)} bytes).</p>`;
    else if (/\.(md|markdown|txt)$/i.test(m.path)) html += `<div class="memo">${markdown(m.text)}</div>`;
    else html += `<pre class="memo">${esc(m.text)}</pre>`;
  }
  if (doc.paths.length) {
    html += `<h3>Other sources</h3><ul>${doc.paths.map((p) => `<li><code>${esc(p.path)}</code>${p.exists ? "" : ' <span class="bad">missing</span>'}</li>`).join("")}</ul>`;
  }
  if (doc.history.length) {
    html += `<details><summary>History, ${doc.history.length} events</summary><ol class="history">${doc.history.map((h) =>
      `<li><span class="muted">${esc(String(h.at ?? "").slice(0, 16).replace("T", " "))}</span> ${esc(h.kind)} by ${esc(h.who)}${h.detail ? `: ${esc(h.detail)}` : ""}</li>`).join("")}</ol></details>`;
  }
  return html;
}

export function runsHTML(runs) {
  if (!runs.length) return "";
  const rows = runs.map((r) => `<tr class="st-${esc(r.standing)}" title="${esc(r.reason || "")}">
    <td>${esc(r.id)}</td><td>${esc(r.standing)}</td><td class="num">${fmt(r.objective)}</td>
    <td>${esc(r.epoch)}${r.current ? "" : " (old)"}</td><td>${esc(r.session)}</td></tr>`).join("");
  return `<h3>Runs, ${runs.length}</h3><table class="runs"><thead><tr><th>Row</th><th>Standing</th><th class="num">Objective</th><th>Epoch</th><th>Session</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// A group of unlinked entries: `members` are ids, `titles` maps id to title.
export function bucketHTML(label, members, titles) {
  return `<h2>${esc(label.split("\n")[0])}</h2>
    <p class="muted">No stored link joins these entries to any other shown entry.</p>
    <ul class="links">${members.map((m) => `<li><a href="#graph" data-entry="${esc(m)}">${esc(m)}</a> ${esc(titles.get(m))}</li>`).join("")}</ul>`;
}
