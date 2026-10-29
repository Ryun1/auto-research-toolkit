// The knowledge graph: entries, the links the record stores, and a side
// panel per entry. Links come from the server (links.links in core); this
// file only lays them out and filters what is shown.

import { fetchJSON, onChange, watch } from "./app.js";
import { BUCKET, elements as buildElements, fillOf, pickHit, searchHits } from "./graphdata.js";
import { PANEL_READS, stale } from "./live.js";
import { esc } from "./md.js";
import { bucketHTML, panelHTML } from "./panel.js";

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

function elements(doc) {
  return buildElements(doc, state);
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
  const query = $("graph-search").value;
  const cy = state.cy;
  if (!cy) return;
  cy.elements().removeClass("dim hit");
  if (!query.trim()) { $("search-count").textContent = ""; return; }
  const hits = searchHits(state.doc.nodes, query);
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
  if (id.startsWith(BUCKET)) { state.panelHTML = null; return showBucket(id); }
  if (!refresh) {
    state.panelHTML = null;
    panel.scrollTop = 0;
    $("panel-body").innerHTML = `<p class="muted">Reading ${esc(id)}</p>`;
  }
  const ticket = (state.panelTicket = (state.panelTicket || 0) + 1);
  try {
    const doc = await fetchJSON(`entry/${encodeURIComponent(id)}`);
    if (state.selected !== id || state.panelTicket !== ticket) return;
    const html = panelHTML(doc, (status) => fillOf(state.doc, status));
    // A refresh that changes nothing leaves the panel alone: an open History,
    // a selection or focus inside it survive the clock and unrelated rows.
    if (refresh && html === state.panelHTML) return;
    const top = panel.scrollTop;
    const open = refresh && !!panel.querySelector("details[open]");
    $("panel-body").innerHTML = html;
    state.panelHTML = html;
    if (open) panel.querySelectorAll("details").forEach((d) => { d.open = true; });
    panel.scrollTop = refresh ? top : 0;
  } catch (err) {
    if (state.selected !== id || state.panelTicket !== ticket) return;
    state.panelHTML = null;
    $("panel-body").innerHTML = `<p class="bad">${esc(err.message)}</p>`;
  }
}

function showBucket(id) {
  const node = state.cy.getElementById(id);
  const titles = new Map(state.doc.nodes.map((n) => [n.id, n.title]));
  $("panel-body").innerHTML = bucketHTML(node.data("label"), node.data("members"), titles);
}

// -- wiring --------------------------------------------------------------------

function setUp() {
  $("graph-search").addEventListener("input", search);
  $("graph-search").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !state.cy) return;
    const id = pickHit(state.cy.nodes(".hit").map((n) => n.id()), $("graph-search").value);
    const hit = id && state.cy.getElementById(id);
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
  onChange((changed) => {
    if (!state.selected || state.selected.startsWith(BUCKET)) return;
    if (stale(PANEL_READS, changed)) select(state.selected, true);
  });
  document.addEventListener("tabshown", (event) => {
    if (event.detail !== "graph" || !state.cy) return;
    state.cy.resize();
    if (state.laidOutHidden) { state.cy.fit(undefined, 24); state.laidOutHidden = false; }
  });
}

setUp();
