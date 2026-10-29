// Live-update bookkeeping: which documents a change makes stale, and which
// response is the newest. No DOM, so it is tested under node.

// Which resources each endpoint reads (mirrors api.READS on the server).
export const READS = {
  summary: ["entries", "claims", "iterations", "runs", "inbox", "skills", "state", "holdout", "clock"],
  board: ["entries", "claims", "iterations", "runs", "inbox", "skills", "state", "holdout", "clock"],
  entries: ["entries", "claims", "state"],
  graph: ["entries", "claims", "state", "runs", "clock"],
};

// The open entry panel reads more than the graph does: memos, guides, rows.
export const PANEL_READS = ["entries", "claims", "state", "runs", "inbox", "guides", "skills", "clock"];

// Whether a change to `changed` (resource names, or null after a reconnect,
// when anything may have moved) makes a document reading `reads` stale. An
// endpoint with no known reads is refetched on every change.
export function stale(reads, changed) {
  if (changed === null) return true;
  return (reads || changed).some((r) => changed.includes(r));
}

// Responses can arrive out of order: hand each request a ticket and draw a
// response only if its ticket is still the newest for that key.
export class Tickets {
  constructor() { this.latest = new Map(); }
  issue(key) {
    const ticket = (this.latest.get(key) || 0) + 1;
    this.latest.set(key, ticket);
    return ticket;
  }
  current(key, ticket) { return this.latest.get(key) === ticket; }
}
