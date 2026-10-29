// What the knowledge graph draws, from the server's graph document and the
// view's filters: no DOM and no Cytoscape, so it is tested under node.

export const BUCKET = "bucket:";
// Fewer unlinked entries than this with one track and status stay loose:
// a group of one or two hides more than it tidies.
export const BUCKET_MIN = 3;

// Every entry upstream or downstream of `id` along the stored links.
export function lineage(links, id) {
  const up = new Map(), down = new Map();
  for (const l of links) {
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

// The entries the filters leave: `view` is {tracks, statuses, focus}.
export function visibleNodes(doc, view) {
  let nodes = doc.nodes.filter((n) => view.tracks.has(n.track) && view.statuses.has(n.status));
  if (view.focus) {
    const keep = lineage(doc.links, view.focus);
    nodes = nodes.filter((n) => keep.has(n.id));
  }
  return nodes;
}

// Cytoscape elements for the shown entries: nodes, groups of unlinked
// entries, and the links between shown entries.
export function elements(doc, view) {
  const nodes = visibleNodes(doc, view);
  const shown = new Set(nodes.map((n) => n.id));
  const links = doc.links.filter((l) => shown.has(l.src) && shown.has(l.dst));
  const linked = new Set(links.flatMap((l) => [l.src, l.dst]));
  const out = [];
  const buckets = new Map();
  // A claimed entry is never grouped away: who holds what is the point.
  for (const n of nodes) {
    if (!linked.has(n.id) && !view.focus && !n.claim) {
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
      fill: fillOf(doc, b.status),
    } });
  }
  for (const l of links) {
    out.push({ group: "edges", data: { id: `${l.kind}:${l.src}>${l.dst}`, source: l.src, target: l.dst, kind: l.kind } });
  }
  return out;
}

export function nodeData(doc, n) {
  const claim = n.claim;
  return {
    id: n.id, status: n.status, title: n.title,
    fill: fillOf(doc, n.status),
    claimed: claim ? (claim.overrun ? "overrun" : "live") : "",
    label: claim ? `${n.id}\n${claim.session}${claim.overrun ? ", overrun" : ""}` : n.id,
  };
}

export function fillOf(doc, status) {
  return ((doc && doc.statuses[status]) || {}).fill || "#eeeeee";
}

// Ids of the entries a search matches: id, title or hypothesis. A record may
// hold a list where a string belongs; search what it says.
export function searchHits(nodes, query) {
  const q = query.trim().toLowerCase();
  if (!q) return new Set();
  return new Set(nodes.filter((n) => n.id.toLowerCase().startsWith(q)
    || String(n.title ?? "").toLowerCase().includes(q)
    || String(n.hypothesis ?? "").toLowerCase().includes(q)).map((n) => n.id));
}

// The hit Enter opens: an exact id beats a prefix, which beats a mention in
// a title or hypothesis. `ids` keeps the drawn order for the last case.
export function pickHit(ids, query) {
  const q = query.trim().toLowerCase();
  return ids.find((id) => id.toLowerCase() === q)
    || ids.find((id) => id.toLowerCase().startsWith(q)) || ids[0];
}
