// md.js: memos are agent-written, so the renderer must never pass HTML
// through, and must stay fast on whatever an agent writes.
import assert from "node:assert/strict";
import { test } from "node:test";

import { esc, markdown } from "../../src/autoresearch/ui/static/md.js";

test("markup in a memo is escaped, never rendered", () => {
  const html = markdown('<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">');
  assert.ok(!html.includes("<script"), html);
  assert.ok(!html.includes("<img"), html);
  assert.ok(html.includes("&lt;script&gt;"), html);
});

test("only http(s) links become anchors, and an href cannot break out", () => {
  const html = markdown('[a](javascript:alert(1)) [b](https://e.example/?q="><x) [c](data:text/html,x)');
  assert.equal((html.match(/<a /g) || []).length, 1, html);
  assert.ok(html.includes('href="https://e.example/?q=&quot;&gt;&lt;x"'), html);
  assert.ok(html.includes('rel="noopener noreferrer"'), html);
});

test("inline code is escaped and not re-parsed as emphasis", () => {
  assert.equal(markdown("`<b>*x*</b>`"), "<p><code>&lt;b&gt;*x*&lt;/b&gt;</code></p>");
});

test("frontmatter is dropped; headings sit below the panel's own", () => {
  assert.equal(markdown("---\ntags: [a]\n---\n# Title"), "<h3>Title</h3>");
});

test("a table keeps a row that starts with dashes", () => {
  const html = markdown("| flag | use |\n| --- | :-: |\n| --epoch | pick one |");
  assert.ok(html.includes("<td>--epoch</td>"), html);
  assert.equal((html.match(/<tr>/g) || []).length, 2, html);
});

test("a table without a separator, or only separators, still renders", () => {
  assert.ok(markdown("| a |\n| b |").includes("<td>b</td>"));
  assert.ok(markdown("| --- |\n| --- |").includes("<table>"));
});

test("fenced code keeps its text verbatim and escaped", () => {
  assert.equal(markdown("```\n<a> & b\n```"), "<pre><code>&lt;a&gt; &amp; b</code></pre>");
});

test("lists, quotes and rules", () => {
  assert.equal(markdown("- one\n- two"), "<ul><li>one</li><li>two</li></ul>");
  assert.equal(markdown("1. one\n2. two"), "<ol><li>one</li><li>two</li></ol>");
  assert.equal(markdown("> said"), "<blockquote>said</blockquote>");
  assert.equal(markdown("a\n\n---\n\nb"), "<p>a</p>\n<hr>\n<p>b</p>");
});

test("hostile whitespace does not freeze the page", () => {
  const started = Date.now();
  markdown("| a |\n|---" + " ".repeat(250_000) + "\n| b |");
  markdown("x".repeat(250_000));
  assert.ok(Date.now() - started < 500, `${Date.now() - started} ms`);
});

test("esc handles every value a record can hold", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(7), "7");
  assert.equal(esc(["<a>", "b"]), "&lt;a&gt;,b");
  assert.equal(esc(`"'&<>`), "&quot;'&amp;&lt;&gt;");
});
