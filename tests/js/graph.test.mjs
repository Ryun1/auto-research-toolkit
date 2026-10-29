// graphdata.js and live.js: what the graph draws, what search finds, and
// which documents a change makes stale.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BUCKET, BUCKET_MIN, elements, lineage, pickHit, searchHits,
} from "../../src/autoresearch/ui/static/graphdata.js";
import { PANEL_READS, READS, stale, Tickets } from "../../src/autoresearch/ui/static/live.js";

const node = (id, over = {}) => ({ id, title: `${id} title`, track: "research", status: "queued", hypothesis: "", claim: null, ...over });

function doc(nodes, links = []) {
  return {
    tracks: [{ id: "research", title: "Hypothesis Queue" }, { id: "harness", title: "Harness Debt" }],
    statuses: { queued: { fill: "#e8e8e8" }, confirmed: { fill: "#d3f0d3" } },
    nodes, links,
  };
}

const all = (d, focus = null) => ({
  tracks: new Set(d.tracks.map((t) => t.id)), statuses: new Set(Object.keys(d.statuses)), focus,
});
const ids = (els, group) => els.filter((e) => e.group === group).map((e) => e.data.id).sort();

test("lineage walks both directions and never sideways", () => {
  const links = [{ src: "A", dst: "B" }, { src: "B", dst: "C" }, { src: "X", dst: "B" }, { src: "C", dst: "D" }, { src: "X", dst: "Y" }];
  assert.deepEqual([...lineage(links, "C")].sort(), ["A", "B", "C", "D", "X"]);
  assert.deepEqual([...lineage(links, "Z")], ["Z"]);
});

test("lineage survives a cycle", () => {
  assert.deepEqual([...lineage([{ src: "A", dst: "B" }, { src: "B", dst: "A" }], "A")].sort(), ["A", "B"]);
});

test("unlinked entries group by track and status once there are enough", () => {
  const d = doc([node("Q1"), node("Q2"), node("Q3"), node("Q4", { status: "confirmed" }), node("Q5"), node("Q6")],
    [{ src: "Q5", dst: "Q6", kind: "cites" }]);
  const els = elements(d, all(d));
  const bucket = els.find((e) => e.data.bucket);
  assert.equal(bucket.data.id, `${BUCKET}research:queued`);
  assert.deepEqual(bucket.data.members, ["Q1", "Q2", "Q3"]);
  assert.match(bucket.data.label, /^3 queued, unlinked\nHypothesis Queue$/);
  assert.equal(bucket.data.claimed, "", "a group never wears a claim ring");
  // Q4 is alone in its status, below BUCKET_MIN, so it stays a node.
  assert.ok(BUCKET_MIN > 1);
  assert.deepEqual(ids(els, "nodes"), ["Q4", "Q5", "Q6", bucket.data.id].sort());
  assert.deepEqual(ids(els, "edges"), ["cites:Q5>Q6"]);
});

test("a claimed entry is never grouped away", () => {
  const d = doc([node("Q1"), node("Q2"), node("Q3"), node("Q4", { claim: { session: "alice", overrun: { meter: "max_hours" } } })]);
  const els = elements(d, all(d));
  const claimed = els.find((e) => e.data.id === "Q4");
  assert.equal(claimed.data.claimed, "overrun");
  assert.equal(claimed.data.label, "Q4\nalice, overrun");
  assert.deepEqual(els.find((e) => e.data.bucket).data.members, ["Q1", "Q2", "Q3"]);
});

test("filters drop entries and the links that touch them", () => {
  const d = doc([node("Q1"), node("Q2", { status: "confirmed" }), node("H1", { track: "harness" })],
    [{ src: "Q1", dst: "Q2", kind: "parent" }, { src: "H1", dst: "Q1", kind: "cites" }]);
  const view = { ...all(d), statuses: new Set(["queued"]) };
  const els = elements(d, view);
  assert.deepEqual(ids(els, "nodes"), ["H1", "Q1"]);
  assert.deepEqual(ids(els, "edges"), ["cites:H1>Q1"]);
  const noHarness = elements(d, { ...all(d), tracks: new Set(["research"]) });
  assert.deepEqual(ids(noHarness, "nodes"), ["Q1", "Q2"]);
});

test("focus shows one lineage and never groups", () => {
  const d = doc([node("Q1"), node("Q2"), node("Q3"), node("Q4"), node("Q5")],
    [{ src: "Q1", dst: "Q2", kind: "parent" }]);
  const els = elements(d, all(d, "Q2"));
  assert.deepEqual(ids(els, "nodes"), ["Q1", "Q2"]);
  assert.ok(!els.some((e) => e.data.bucket));
});

test("search reads id, title and hypothesis, whatever type the record holds", () => {
  const nodes = [node("Q1", { hypothesis: ["screen", "rate"] }), node("Q12", { title: 42 }),
    node("Q2", { title: null, hypothesis: "mentions Q1" })];
  assert.deepEqual([...searchHits(nodes, " q1 ")].sort(), ["Q1", "Q12", "Q2"]);
  assert.deepEqual([...searchHits(nodes, "screen")], ["Q1"]);
  assert.deepEqual([...searchHits(nodes, "42")], ["Q12"]);
  assert.equal(searchHits(nodes, "   ").size, 0);
});

test("Enter opens an exact id, then a prefix, then the first mention", () => {
  assert.equal(pickHit(["Q293", "Q2940", "Q294"], "q294"), "Q294");
  assert.equal(pickHit(["Q293", "Q2940"], "Q294"), "Q2940");
  assert.equal(pickHit(["Q293", "Q12"], "screen"), "Q293");
  assert.equal(pickHit([], "x"), undefined);
});

test("a change refetches only the documents that read it; a reconnect refetches all", () => {
  assert.ok(stale(READS.graph, ["entries"]));
  assert.ok(!stale(READS.graph, ["inbox"]), "a memo edit does not re-lay out the graph");
  assert.ok(stale(PANEL_READS, ["inbox"]), "but it does re-read the open panel");
  assert.ok(stale(READS.summary, ["clock"]) && stale(READS.graph, ["clock"]));
  assert.ok(!stale(READS.entries, ["clock"]));
  assert.ok(stale(READS.entries, null));
  assert.ok(stale(undefined, ["anything"]), "an endpoint with unknown reads refetches");
});

test("only the newest response per key is drawn", () => {
  const tickets = new Tickets();
  const first = tickets.issue("graph");
  const second = tickets.issue("graph");
  const other = tickets.issue("summary");
  assert.ok(!tickets.current("graph", first));
  assert.ok(tickets.current("graph", second));
  assert.ok(tickets.current("summary", other));
});
