// ar ui: the page shell. Fetches JSON the server computes with the CLI's own
// functions, and refetches what the event stream says changed. Nothing here
// decides a verdict; it only lays out what the server sent.

import { fmt } from "./format.js";
import { READS, stale, Tickets } from "./live.js";

export { fmt };

const $ = (id) => document.getElementById(id);

const listeners = new Map(); // endpoint -> [fn]
const changeHooks = [];      // fn(changed names), for documents fetched by hand
const tickets = new Tickets();
const errors = new Map();    // endpoint -> message of its last failure

export async function fetchJSON(name) {
  const response = await fetch(`/api/${name}`, { cache: "no-store" });
  const doc = await response.json();
  if (!response.ok) throw new Error(doc.error || `${name}: HTTP ${response.status}`);
  return doc;
}

// Call fn(doc) now and after every change to what `name` reads.
export function watch(name, fn) {
  if (!listeners.has(name)) listeners.set(name, []);
  listeners.get(name).push(fn);
  refresh(name);
}

// Call fn(changedNames) on every change event and after a reconnect (with
// null): for documents a view fetches itself, such as the open entry panel.
export function onChange(fn) {
  changeHooks.push(fn);
}

async function refresh(name) {
  // Responses can arrive out of order; only the newest request is drawn.
  const ticket = tickets.issue(name);
  try {
    const doc = await fetchJSON(name);
    if (!tickets.current(name, ticket)) return;
    showError(name, null);
    for (const fn of listeners.get(name) || []) fn(doc);
  } catch (err) {
    if (tickets.current(name, ticket)) showError(name, err.message);
  }
}

function showError(name, message) {
  if (message) errors.set(name, message); else errors.delete(name);
  const box = $("error");
  box.hidden = !errors.size;
  box.textContent = errors.size ? `Could not read the record: ${[...errors.values()].join("; ")}` : "";
}

// -- header -----------------------------------------------------------------

function renderSummary(doc) {
  document.title = `${doc.domain} · ar ui`;
  $("domain").textContent = `${doc.domain}, goal ${doc.goal.id}`;
  const objective = $("objective");
  objective.textContent = doc.goal.objective + " ";
  const direction = document.createElement("span");
  direction.className = "direction";
  direction.textContent = doc.goal.direction;
  objective.append(direction);

  const best = $("best");
  best.className = "";
  if (doc.best) {
    best.textContent = fmt(doc.best.objective) + " ";
    const where = document.createElement("small");
    where.textContent = doc.best.entry || doc.best.run;
    best.append(where);
  } else {
    best.textContent = "none yet";
  }

  const distance = $("distance");
  distance.className = "";
  distance.title = "";
  if (doc.best && doc.best.distance !== null && doc.best.distance !== undefined) {
    const pct = (doc.best.distance * 100).toFixed(2);
    distance.textContent = `${doc.best.distance > 0 ? "+" : ""}${pct}%`;
    if (doc.best.met && !doc.best.unmet_required_gates.length) {
      distance.className = "met";
      distance.title = "Goal met";
    } else if (doc.best.met) {
      distance.title = `Not a win: required gates not passed (${doc.best.unmet_required_gates.join(", ")})`;
    }
  } else {
    distance.textContent = "no distance";
  }

  const target = $("target");
  target.textContent = doc.target === null ? "not probed" : fmt(doc.target);
  target.title = doc.target_error || (doc.goal.moving ? "A moving target" : "");
  if (doc.goal.moving && doc.target !== null) {
    const small = document.createElement("small");
    small.textContent = " moving";
    target.append(small);
  }
}

// -- tabs -------------------------------------------------------------------

function selectTab(button) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const on = tab === button;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
  }
  document.dispatchEvent(new CustomEvent("tabshown", { detail: button.id.slice(4) }));
  history.replaceState(null, "", `#${button.id.slice(4)}`);
}

function setUpTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
      next.focus();
      selectTab(next);
    });
  }
  const wanted = $(`tab-${location.hash.slice(1)}`);
  selectTab(wanted || tabs[0]);
}

// -- live updates -----------------------------------------------------------

function setLink(state, text) {
  $("link").dataset.state = state;
  $("link-text").textContent = text;
}

function connect() {
  const events = new EventSource("/api/events");
  let first = true;
  events.addEventListener("hello", () => {
    setLink("live", "Live");
    // A reconnect may have missed changes: refetch everything once.
    if (!first) {
      for (const name of listeners.keys()) refresh(name);
      for (const fn of changeHooks) fn(null);
    }
    first = false;
  });
  events.addEventListener("change", (event) => {
    const changed = JSON.parse(event.data);
    for (const name of listeners.keys()) {
      if (stale(READS[name], changed)) refresh(name);
    }
    for (const fn of changeHooks) fn(changed);
  });
  events.onerror = () => setLink("down", "Disconnected, retrying");
}

setUpTabs();
watch("summary", renderSummary);
connect();
