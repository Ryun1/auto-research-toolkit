// Number formatting for the page. No DOM, so it is tested under node.

// Always safe to put into HTML: anything that is not a finite number reads
// "none", so a string in an agent-written row never reaches the page.
export function fmt(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "none";
  if (Math.abs(value) >= 1) return value.toLocaleString("en", { maximumFractionDigits: Math.abs(value) >= 1000 ? 0 : 2 });
  return value.toPrecision(4);
}
