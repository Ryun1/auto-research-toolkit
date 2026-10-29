// panel.js and format.js: every server value in the side panel is
// agent-written, so none may reach the page as markup.
import assert from "node:assert/strict";
import { test } from "node:test";

import { fmt } from "../../src/autoresearch/ui/static/format.js";
import { bucketHTML, panelHTML, runsHTML } from "../../src/autoresearch/ui/static/panel.js";

const EVIL = '<img src=x onerror="alert(1)">';

function doc(over = {}) {
  return {
    entry: { id: "Q1", track: "research", status: "queued", title: "t", ...over.entry },
    upstream: [], downstream: [], runs: [], memo: null, paths: [], history: [],
    ...over,
  };
}

// A tag the panel itself writes, or `<` from an escaped value: anything else
// that opens a tag came from the record.
function foreignTags(html) {
  const ours = /^<\/?(p|span|h2|h3|strong|br|ul|ol|li|a|button|table|thead|tbody|tr|th|td|div|pre|code|details|summary|em|blockquote|hr)[\s>]/;
  return html.split(/(?=<)/).filter((part) => part.startsWith("<") && !ours.test(part));
}

test("hostile text in every field stays text", () => {
  const html = panelHTML({
    entry: {
      id: EVIL, track: EVIL, status: EVIL, title: EVIL, hypothesis: EVIL, prediction: EVIL,
      claim: { session: EVIL, at: EVIL, why: EVIL, overrun: { meter: EVIL, spent: EVIL, cap: EVIL } },
      result: { verdict: EVIL, closure_kind: EVIL, session: EVIL, at: EVIL, summary: EVIL, reopen_condition: EVIL },
      gates: [{ name: EVIL, state: EVIL, required: true }],
    },
    upstream: [{ id: EVIL, kind: "cites", title: EVIL }],
    downstream: [{ id: EVIL, kind: "parent", title: EVIL }],
    runs: [{ id: EVIL, standing: EVIL, reason: EVIL, objective: EVIL, epoch: EVIL, current: false, session: EVIL }],
    memo: { path: `${EVIL}.md`, exists: true, text: EVIL, size: 1 },
    paths: [{ path: EVIL, exists: false }],
    history: [{ at: EVIL, kind: EVIL, who: EVIL, detail: EVIL }],
  }, () => `red;"><script>x</script>`);
  assert.deepEqual(foreignTags(html), []);
  assert.ok(!html.includes("onerror=\""), "an attribute escaped its quotes");
  assert.ok(html.includes("&lt;img"));
});

test("values a record should not hold do not break the panel", () => {
  const html = panelHTML(doc({
    entry: { hypothesis: ["a", "b"], claim: { session: null, at: null } },
    runs: [{ id: "r", standing: "ok", objective: null, epoch: "1", current: true, session: null }],
    history: [{ at: null, kind: "filed", who: null }],
  }), () => "#fff");
  assert.ok(html.includes("<p>a,b</p>"), html);
  assert.ok(html.includes("Claimed by <strong></strong>"), html);
});

test("a memo renders by its kind", () => {
  const md = panelHTML(doc({ memo: { path: "inbox/m.md", exists: true, text: "**b**", size: 5 } }), () => "");
  assert.ok(md.includes("<strong>b</strong>"));
  const raw = panelHTML(doc({ memo: { path: "inbox/m.json", exists: true, text: "**b**", size: 5 } }), () => "");
  assert.ok(raw.includes('<pre class="memo">**b**</pre>'));
  const big = panelHTML(doc({ memo: { path: "inbox/m.md", exists: true, text: null, size: 300000 } }), () => "");
  assert.ok(big.includes("Too large to show here (300,000 bytes)"));
  const gone = panelHTML(doc({ memo: { path: "inbox/m.md", exists: false, text: null, size: null } }), () => "");
  assert.ok(gone.includes("The memo file is missing."));
});

test("links name their kind from the reader's side", () => {
  const html = panelHTML(doc({
    upstream: [{ id: "Q0", kind: "parent", title: "root" }],
    downstream: [{ id: "Q2", kind: "cites", title: "later" }],
  }), () => "");
  assert.ok(html.includes('parent <a href="#graph" data-entry="Q0">Q0</a> root'));
  assert.ok(html.includes('cited by <a href="#graph" data-entry="Q2">Q2</a> later'));
  assert.ok(html.includes('data-focus="Q1"'));
});

test("run rows keep their standing distinct and mark old epochs", () => {
  const html = runsHTML([
    { id: "a", standing: "ok", reason: "", objective: 12.5, epoch: "2", current: true, session: "s" },
    { id: "b", standing: "suspect", reason: "far past", objective: 3, epoch: "1", current: false, session: "s" },
  ]);
  assert.ok(html.includes('class="st-ok"') && html.includes('class="st-suspect" title="far past"'));
  assert.ok(html.includes("<td>1 (old)</td>") && html.includes("<td>2</td>"));
  assert.equal(runsHTML([]), "");
});

test("a group lists its members escaped", () => {
  const html = bucketHTML("3 queued, unlinked\nHypothesis Queue", ["Q1"], new Map([["Q1", EVIL]]));
  assert.ok(html.startsWith("<h2>3 queued, unlinked</h2>"));
  assert.deepEqual(foreignTags(html), []);
});

test("fmt prints only finite numbers", () => {
  assert.equal(fmt(1234567.8), "1,234,568");
  assert.equal(fmt(12.345), "12.35");
  assert.equal(fmt(0.000123456), "0.0001235");
  for (const value of [EVIL, "5", null, undefined, NaN, Infinity, [1], { a: 1 }, true]) {
    assert.equal(fmt(value), "none", String(value));
  }
});
