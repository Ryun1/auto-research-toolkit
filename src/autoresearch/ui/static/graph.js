// The knowledge graph: entries, the links the record stores, and a side
// panel per entry. Links come from the server (links.links in core); this
// file only lays them out and filters what is shown.

import { fetchJSON, fmt, onChange, watch } from "./app.js";
import { esc, markdown } from "./md.js";

cytoscape.use(cytoscapeDagre);

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const state = {
  doc: null,
  cy: null,
  shape: "",          // node ids and links of the drawn graph, to tell a restyle from a re-layout
  selected: null,     // entry id or bucket id in the panel
  focus: null,        // entry id whose lineage alone is shown
  tracks: new Set(),  // shown tracks
  statuses: new Set(),
  filtersSet: false,  // the filters start with everything shown, once
  laidOutHidden: false, // a layout run while the tab was hidden has no size to fit
};

const BUCKET = "bucket:";
// Fewer unlinked entries than this with one track and status stay loose:
// a group of one or two hides more than it tidies.
const BUCKET_MIN = 3;

// -- elements -----------------------------------------------------------------

function visibleNodes(doc) {
  let nodes = doc.nodes.filter((n) => state.tracks.has(n.track) && state.statuses.has(n.status));
  if (state.focus) {
    const keep = lineage(doc, state.focus);
    nodes = nodes.filter((n) => keep.has(n.id));
  }
  return nodes;
}

// Every entry upstream or downstream of `id` along the stored links.
function lineage(doc, id) {
  const up = new Map(), down = new Map();
  for (const l of doc.links) {
    (down.get(l.src) || down.set(l.src, []).get(l.src)).push(l.dst);
    (up.get(l.dst) || up.set(l.dst, []).get(l.dst)).push(l.src);
  }
  const seen = new Set([id]);
  for (const graph of [up, down]) {
    const stack = [id];
    while (stack.length) {
      for (const next of graph.get(stack.pop()) || []) {
        if (!seen.has(next)) { seen.add(next); stack.push(next); }
      }
    }
  }
  return seen;
}

function elements(doc) {
  const nodes = visibleNodes(doc);
  const shown = new Set(nodes.map((n) => n.id));
  const links = doc.links.filter((l) => shown.has(l.src) && shown.has(l.dst));
  const linked = new Set(links.flatMap((l) => [l.src, l.dst]));
  const out = [];
  const buckets = new Map();
  // A claimed entry is never grouped away: who holds what is the point.
  for (const n of nodes) {
    if (!linked.has(n.id) && !state.focus && !n.claim) {
      const key = `${BUCKET}${n.track}:${n.status}`;
      if (!buckets.has(key)) buckets.set(key, { track: n.track, status: n.status, members: [] });
      buckets.get(key).members.push(n);
    }
  }
  for (const [key, b] of buckets) {
    if (b.members.length < BUCKET_MIN) buckets.delete(key);
  }
  const grouped = new Set([...buckets.values()].flatMap((b) => b.members.map((m) => m.id)));
  for (const n of nodes) {
    if (!grouped.has(n.id)) out.push({ group: "nodes", data: nodeData(doc, n) });
  }
  for (const [key, b] of buckets) {
    const track = doc.tracks.find((t) => t.id === b.track);
    out.push({ group: "nodes", data: {
      id: key, bucket: true, claimed: "", status: b.status, members: b.members.map((m) => m.id),
      label: `${b.members.length} ${b.status}, unlinked\n${track ? track.title : b.track}`,
      fill: (doc.statuses[b.status] || {}).fill || "#eeeeee",
    } });
  }
  for (const l of links) {
    out.push({ group: "edges", data: { id: `${l.kind}:${l.src}>${l.dst}`, source: l.src, target: l.dst, kind: l.kind } });
  }
  return out;
}

function nodeData(doc, n) {
  const claim = n.claim;
  return {
    id: n.id, status: n.status, title: n.title,
    fill: (doc.statuses[n.status] || {}).fill || "#eeeeee",
    claimed: claim ? (claim.overrun ? "overrun" : "live") : "",
    label: claim ? `${n.id}\n${claim.session}${claim.overrun ? ", overrun" : ""}` : n.id,
  };
}

function style() {
  const ink = css("--ink"), soft = css("--ink-soft"), live = css("--live"), warn = css("--warn");
  return [
    { selector: "node", style: {
      shape: "round-rectangle", width: 54, height: 26,
      "background-color": "data(fill)", "border-width": 1, "border-color": "#6b7785",
      label: "data(label)", "font-size": 11, "font-family": css("--sans"), color: "#1c2b3a",
      "text-valign": "center", "text-halign": "center", "text-wrap": "wrap",
    } },
    { selector: "node[claimed != '']", style: {
      "border-width": 3, "border-color": live, height: 36, width: 124, "font-size": 10,
    } },
    { selector: "node[claimed = 'overrun']", style: { "border-color": warn, "border-width": 4 } },
    { selector: "node[?bucket]", style: {
      shape: "rectangle", width: 150, height: 40, "border-style": "dashed", "font-size": 11,
    } },
    { selector: "edge", style: {
      width: 1, "line-color": soft, "target-arrow-color": soft, "target-arrow-shape": "triangle",
      "arrow-scale": 0.7, "curve-style": "bezier", opacity: 0.55,
    } },
    { selector: "edge[kind = 'parent']", style: { width: 2, "line-color": ink, "target-arrow-color": ink, opacity: 0.9 } },
    { selector: "edge[kind = 'derived']", style: { width: 1.5, "line-style": "dashed", "line-color": ink, "target-arrow-color": ink, opacity: 0.8 } },
    { selector: "edge[kind = 'supersedes']", style: { width: 3, "line-color": "#7a5ea8", "target-arrow-color": "#7a5ea8", opacity: 0.9 } },
    { selector: "edge[kind = 'related']", style: { "line-style": "dotted", "target-arrow-shape": "none", opacity: 0.8 } },
    { selector: ".dim", style: { opacity: 0.15 } },
    { selector: "edge.dim", style: { opacity: 0.05 } },
    { selector: ".hit", style: { "border-width": 3, "border-color": ink } },
    { selector: "node:selected", style: { "overlay-color": live, "overlay-opacity": 0.25, "overlay-padding": 6 } },
    { selector: "edge.lit", style: { opacity: 1, width: 2, "line-color": live, "target-arrow-color": live } },
  ];
}

const LAYOUT = { name: "dagre", rankDir: "TB", nodeSep: 14, rankSep: 60, edgeSep: 6, animate: false, fit: true, padding: 24 };

// -- drawing ------------------------------------------------------------------

function draw(doc) {
  const els = elements(doc);
  const shape = JSON.stringify(els.map((e) => e.data.id).sort());
  if (state.cy && shape === state.shape) {
    // Same nodes and links: restyle in place, keep the layout and the view.
    for (const e of els) if (e.group === "nodes") state.cy.getElementById(e.data.id).data(e.data);
    return;
  }
  const started = performance.now();
  if (!state.cy) {
    state.cy = cytoscape({ container: $("graph-canvas"), style: style(),
      minZoom: 0.05, maxZoom: 3, boxSelectionEnabled: false });
    state.cy.on("tap", "node", (event) => select(event.target.id()));
    state.cy.on("tap", (event) => { if (event.target === state.cy) select(null); });
    state.cy.on("mouseover", "node", (event) => {
      const d = event.target.data();
      $("graph-canvas").title = d.bucket ? d.label.replace("\n", ", ") : `${d.id}: ${d.title}`;
    });
  }
  const cy = state.cy;
  cy.batch(() => { cy.elements().remove(); cy.add(els); });
  cy.layout(LAYOUT).run();
  state.laidOutHidden = $("view-graph").hidden;
  state.shape = shape;
  if (state.selected && cy.getElementById(state.selected).nonempty()) {
    cy.getElementById(state.selected).select();
    lightLinks(state.selected);
  }
  search();
  $("graph-count").textContent = countText(doc, performance.now() - started);
}

function countText(doc, ms) {
  const nodes = state.cy.nodes("[!bucket]").length;
  const bucketed = state.cy.nodes("[?bucket]").reduce((sum, b) => sum + b.data("members").length, 0);
  const links = state.cy.edges().length;
  return `${nodes} drawn, ${bucketed} unlinked in groups, ${links} links (of ${doc.nodes.length} entries)` +
    (ms > 0 ? `, laid out in ${(ms / 1000).toFixed(2)} s` : "");
}

function lightLinks(id) {
  state.cy.edges().removeClass("lit");
  if (id) state.cy.getElementById(id).connectedEdges().addClass("lit");
}

// -- filters, search, focus ----------------------------------------------------

function buildFilters(doc) {
  if (!state.filtersSet) {
    doc.tracks.forEach((t) => state.tracks.add(t.id));
    Object.keys(doc.statuses).forEach((s) => state.statuses.add(s));
    state.filtersSet = true;
  }
  const tracks = $("filter-tracks");
  tracks.replaceChildren(...doc.tracks.map((t) => chip(t.title, state.tracks, t.id,
    doc.nodes.filter((n) => n.track === t.id).length)));
  const present = new Set(doc.nodes.map((n) => n.status));
  const statuses = $("filter-statuses");
  statuses.replaceChildren(...Object.entries(doc.statuses).filter(([s]) => present.has(s)).map(([s, info]) => {
    const label = chip(s, state.statuses, s, doc.nodes.filter((n) => n.status === s).length);
    label.querySelector(".swatch").style.background = info.fill;
    return label;
  }));
}

function chip(text, set, value, count) {
  const label = document.createElement("label");
  label.className = "chip";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = set.has(value);
  box.addEventListener("change", () => {
    if (box.checked) set.add(value); else set.delete(value);
    draw(state.doc);
  });
  const swatch = document.createElement("span");
  swatch.className = "swatch";
  label.append(box, swatch, document.createTextNode(`${text} ${count}`));
  return label;
}

function search() {
  const q = $("graph-search").value.trim().toLowerCase();
  const cy = state.cy;
  if (!cy) return;
  cy.elements().removeClass("dim hit");
  if (!q) { $("search-count").textContent = ""; return; }
  const hits = new Set(state.doc.nodes.filter((n) =>
    n.id.toLowerCase() === q || n.title.toLowerCase().includes(q) || (n.hypothesis || "").toLowerCase().includes(q)
    || n.id.toLowerCase().startsWith(q)).map((n) => n.id));
  cy.nodes().forEach((node) => {
    const d = node.data();
    const match = d.bucket ? d.members.some((m) => hits.has(m)) : hits.has(d.id);
    node.addClass(match ? "hit" : "dim");
  });
  cy.edges().addClass("dim");
  $("search-count").textContent = `${hits.size} match${hits.size === 1 ? "" : "es"}`;
}

function setFocus(id) {
  state.focus = id;
  $("graph-search").value = "";
  $("focus-bar").hidden = !id;
  $("focus-id").textContent = id || "";
  draw(state.doc);
}

// -- side panel ----------------------------------------------------------------

// `refresh` re-reads the open entry after a change and keeps the reader's place.
async function select(id, refresh = false) {
  state.selected = id;
  if (state.cy) {
    state.cy.nodes().unselect();
    if (id && state.cy.getElementById(id).nonempty()) state.cy.getElementById(id).select();
    lightLinks(id);
  }
  const panel = $("panel");
  if (!id) { panel.hidden = true; return; }
  panel.hidden = false;
  if (id.startsWith(BUCKET)) return showBucket(id);
  if (!refresh) {
    panel.scrollTop = 0;
    $("panel-body").innerHTML = `<p class="muted">Reading ${esc(id)}</p>`;
  }
  const ticket = (state.panelTicket = (state.panelTicket || 0) + 1);
  try {
    const doc = await fetchJSON(`entry/${encodeURIComponent(id)}`);
    if (state.selected !== id || state.panelTicket !== ticket) return;
    const top = panel.scrollTop;
    $("panel-body").innerHTML = panelHTML(doc);
    panel.scrollTop = refresh ? top : 0;
  } catch (err) {
    $("panel-body").innerHTML = `<p class="bad">${esc(err.message)}</p>`;
  }
}

function showBucket(id) {
  const node = state.cy.getElementById(id);
  const members = node.data("members");
  const byId = new Map(state.doc.nodes.map((n) => [n.id, n]));
  $("panel-body").innerHTML = `<h2>${esc(node.data("label").split("\n")[0])}</h2>
    <p class="muted">No stored link joins these entries to any other shown entry.</p>
    <ul class="links">${members.map((m) => `<li><a href="#graph" data-entry="${esc(m)}">${esc(m)}</a> ${esc(byId.get(m).title)}</li>`).join("")}</ul>`;
}

const KIND_UP = { parent: "parent", derived: "derived from", supersedes: "supersedes", related: "related", cites: "cites" };
const KIND_DOWN = { parent: "child", derived: "derived by", supersedes: "superseded by", related: "related", cites: "cited by" };

function panelHTML(doc) {
  const e = doc.entry;
  const field = (label, value) => value ? `<h3>${label}</h3><p>${esc(String(value))}</p>` : "";
  const r = e.result;
  let html = `<p class="panel-id"><span class="pill" style="background:${esc(fill(e.status))}">${esc(e.status)}</span> ${esc(e.id)}, ${esc(e.track)}</p>
    <h2>${esc(e.title)}</h2>`;
  if (e.claim) {
    const over = e.claim.overrun;
    html += `<p class="claim${over ? " over" : ""}">Claimed by <strong>${esc(e.claim.session)}</strong> since ${esc(e.claim.at.slice(0, 16).replace("T", " "))}` +
      (over ? `, past its ${esc(over.meter)} ceiling (${fmt(over.spent)} of ${fmt(over.cap)})` : "") +
      (e.claim.why ? `<br><span class="muted">${esc(e.claim.why)}</span>` : "") + "</p>";
  }
  html += field("Hypothesis", e.hypothesis) + field("Prediction", e.prediction);
  if (r) {
    html += `<h3>Verdict</h3><p><strong>${esc(r.verdict)}</strong>` +
      (r.closure_kind ? ` (${esc(r.closure_kind)})` : "") +
      ` by ${esc(r.session)}, ${esc((r.at || "").slice(0, 10))}</p>` +
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
      `<li><span class="muted">${esc((h.at || "").slice(0, 16).replace("T", " "))}</span> ${esc(h.kind)} by ${esc(h.who)}${h.detail ? `: ${esc(h.detail)}` : ""}</li>`).join("")}</ol></details>`;
  }
  return html;
}

function runsHTML(runs) {
  if (!runs.length) return "";
  const rows = runs.map((r) => `<tr class="st-${esc(r.standing)}" title="${esc(r.reason || "")}">
    <td>${esc(r.id)}</td><td>${esc(r.standing)}</td><td class="num">${fmt(r.objective)}</td>
    <td>${esc(r.epoch)}${r.current ? "" : " (old)"}</td><td>${esc(r.session)}</td></tr>`).join("");
  return `<h3>Runs, ${runs.length}</h3><table class="runs"><thead><tr><th>Row</th><th>Standing</th><th class="num">Objective</th><th>Epoch</th><th>Session</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function fill(status) {
  return ((state.doc && state.doc.statuses[status]) || {}).fill || "#eeeeee";
}

// -- wiring --------------------------------------------------------------------

function setUp() {
  $("graph-search").addEventListener("input", search);
  $("graph-search").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !state.cy) return;
    // An exact id beats a prefix, which beats a mention in a title or hypothesis.
    const q = $("graph-search").value.trim().toLowerCase();
    const hits = state.cy.nodes(".hit");
    const hit = hits.filter((n) => n.id().toLowerCase() === q)[0]
      || hits.filter((n) => n.id().toLowerCase().startsWith(q))[0] || hits[0];
    if (hit) { select(hit.id()); state.cy.animate({ center: { eles: hit }, zoom: Math.max(state.cy.zoom(), 1) }, { duration: 200 }); }
  });
  $("graph-fit").addEventListener("click", () => state.cy && state.cy.fit(undefined, 24));
  $("focus-clear").addEventListener("click", () => setFocus(null));
  $("panel-close").addEventListener("click", () => select(null));
  $("panel-body").addEventListener("click", (event) => {
    const link = event.target.closest("[data-entry]");
    if (link) {
      event.preventDefault();
      const id = link.dataset.entry;
      select(id);
      const node = state.cy.getElementById(id);
      if (node.nonempty()) state.cy.animate({ center: { eles: node } }, { duration: 200 });
    }
    const focus = event.target.closest("[data-focus]");
    if (focus) setFocus(focus.dataset.focus);
  });
  watch("graph", (doc) => {
    state.doc = doc;
    buildFilters(doc);
    draw(doc);
  });
  // The panel reads more than the graph does (memos, guides, run rows), so it
  // is re-read on any change to those, keeping the reader's place.
  const PANEL_READS = ["entries", "claims", "state", "runs", "inbox", "guides", "skills", "clock"];
  onChange((changed) => {
    if (!state.selected || state.selected.startsWith(BUCKET)) return;
    if (changed === null || PANEL_READS.some((r) => changed.includes(r))) select(state.selected, true);
  });
  document.addEventListener("tabshown", (event) => {
    if (event.detail !== "graph" || !state.cy) return;
    state.cy.resize();
    if (state.laidOutHidden) { state.cy.fit(undefined, 24); state.laidOutHidden = false; }
  });
}

setUp();
