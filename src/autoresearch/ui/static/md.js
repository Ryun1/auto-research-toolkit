// A small markdown renderer for memos. Memos are written by agents, so the
// text is escaped before any markup is added: no HTML in a memo ever reaches
// the page as HTML, and only http(s) links become anchors.

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

function inline(text) {
  const codes = [];
  let s = esc(text).replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

function table(rows) {
  const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  // Only the second line can be the separator, and only when it is nothing
  // but pipes, dashes and colons: a row like `| --epoch | ... |` is content.
  // Whitespace is dropped first so the test stays linear on any input.
  const body = rows.length > 1 && /^\|?(:?-+:?\|)*:?-+:?\|?$/.test(rows[1].replace(/\s+/g, ""))
    ? [rows[0], ...rows.slice(2)] : rows;
  const [head, ...rest] = body;
  let out = "<table><thead><tr>" + cells(head).map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rest) out += "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
  return out + "</tbody></table>";
}

export function markdown(text) {
  // YAML frontmatter is metadata for tools, not prose for the reader.
  const body = text.replace(/\r\n?/g, "\n").replace(/^---\n[\s\S]*?\n---\n/, "");
  const lines = body.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const body = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
      i++;
      out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);   // the panel owns h1-h2
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      for (; i < lines.length && /^\s*\|/.test(lines[i]); i++) rows.push(lines[i]);
      out.push(table(rows));
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const items = [];
      for (; i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i]); i++) {
        let item = lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "");
        while (i + 1 < lines.length && /^\s{2,}\S/.test(lines[i + 1])
               && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i + 1])) item += " " + lines[++i].trim();
        items.push(`<li>${inline(item)}</li>`);
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      for (; i < lines.length && /^>\s?/.test(lines[i]); i++) quote.push(lines[i].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }
    if (/^\s*$/.test(line)) { i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    const para = [];
    for (; i < lines.length && !/^\s*$/.test(lines[i]) && !/^(```|#{1,6}\s|>|\s*\||\s*([-*+]|\d+[.)])\s+)/.test(lines[i]); i++) {
      para.push(lines[i].trim());
    }
    if (!para.length) { para.push(lines[i].trim()); i++; }
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

export { esc };
