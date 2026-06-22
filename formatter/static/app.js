"use strict";

const inputEl = document.getElementById("input");
const outputEl = document.getElementById("output");
const manualEl = document.getElementById("manual");
const statusEl = document.getElementById("status");
const statusTextEl = statusEl.querySelector(".status-text");
const badgeEl = document.getElementById("badge");
const copyBtn = document.getElementById("copy");
const clearBtn = document.getElementById("clear");
const themeBtn = document.getElementById("theme");
const toastEl = document.getElementById("toast");

let debounceId = null;
let manualKeys = []; // current set of manual-field keys, to avoid clobbering typed values
let lastHtml = ""; // rich HTML for the most recent output (used by Copy)
let lastText = ""; // plain text for the most recent output

/* ---------- helpers ---------- */
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Render output: bold the first two non-empty lines, highlight <...> placeholders
// red, and highlight any flagged values (e.g. an address that failed validation).
function renderOutput(text, flagged = []) {
  const flags = flagged.map(escapeHtml).filter(Boolean);
  const lines = text.split("\n");
  let bolded = 0;
  outputEl.innerHTML = lines.map((line) => {
    let inner = escapeHtml(line).replace(/&lt;[^&\n]*?&gt;/g,
      (m) => `<span class="placeholder">${m}</span>`);
    for (const f of flags) {
      inner = inner.split(f).join(`<span class="flag">${f}</span>`);
    }
    if (line.trim() && bolded < 2) {
      bolded += 1;
      return `<span class="bold">${inner}</span>`;
    }
    return inner;
  }).join("\n");
}

function setStatus(text, level) {
  statusTextEl.textContent = text;
  statusEl.className = "status " + (level || "info");
}

function setBadge(isp) {
  if (isp) {
    badgeEl.textContent = isp;
    badgeEl.classList.remove("hidden");
  } else {
    badgeEl.classList.add("hidden");
  }
}

let toastId = null;
function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  if (toastId) clearTimeout(toastId);
  toastId = setTimeout(() => toastEl.classList.remove("show"), 1800);
}

/* ---------- manual fields ---------- */
// (Re)build inputs only when the set of keys changes, so the user's typed
// values survive re-formatting (mirrors the desktop view).
function renderManualFields(fields, missingKeys) {
  const keys = fields.map((f) => f[0]);
  const sameKeys = keys.length === manualKeys.length &&
    keys.every((k, i) => k === manualKeys[i]);

  if (!sameKeys) {
    const old = getManualValues();
    manualEl.innerHTML = "";
    for (const [key, label] of fields) {
      const wrap = document.createElement("div");
      wrap.className = "field";
      const lbl = document.createElement("label");
      lbl.textContent = label;
      lbl.htmlFor = "m_" + key;
      const inp = document.createElement("input");
      inp.id = "m_" + key;
      inp.dataset.key = key;
      inp.value = old[key] || "";
      inp.placeholder = label;
      inp.addEventListener("input", scheduleFormat);
      wrap.append(lbl, inp);
      manualEl.append(wrap);
    }
    manualKeys = keys;
  }

  const missing = new Set(missingKeys);
  manualEl.querySelectorAll(".field").forEach((field) => {
    const key = field.querySelector("input").dataset.key;
    const isMissing = missing.has(key);
    field.classList.toggle("missing", isMissing);
    field.querySelector("label").classList.toggle("missing", isMissing);
  });
}

function getManualValues() {
  const out = {};
  manualEl.querySelectorAll("input").forEach((inp) => {
    out[inp.dataset.key] = inp.value;
  });
  return out;
}

// Manual values belong to the current ticket; wipe them when the raw input
// changes so a freshly pasted ticket doesn't inherit the previous one's values.
function clearManualValues() {
  manualEl.querySelectorAll("input").forEach((inp) => { inp.value = ""; });
}

/* ---------- format ---------- */
async function format() {
  const raw = inputEl.value.trim();
  let res;
  try {
    res = await fetch("/api/format", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw, manual: getManualValues() }),
    });
  } catch (e) {
    setStatus("Network error — is the server running?", "error");
    return;
  }
  const data = await res.json();

  renderManualFields(data.manual_fields, data.missing_keys);
  renderOutput(data.text || "", data.flagged || []);
  setStatus(data.status, data.level);
  setBadge(data.isp);

  lastText = data.text || "";
  lastHtml = data.html || "";
  copyBtn.disabled = !data.complete;
}

function scheduleFormat() {
  if (debounceId) clearTimeout(debounceId);
  debounceId = setTimeout(format, 250);
}

/* ---------- copy ---------- */
// Legacy fallback for non-secure contexts (plain HTTP over a LAN IP, e.g. when
// deployed via Portainer): the async Clipboard API is unavailable there, so copy
// a selection with document.execCommand. Selecting rich HTML keeps the bold heading.
function legacyCopy() {
  const node = document.createElement("div");
  node.contentEditable = "true";
  node.innerHTML = lastHtml || escapeHtml(lastText);
  node.style.cssText = "position:fixed;left:-9999px;top:0;white-space:pre-wrap;";
  document.body.appendChild(node);

  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(node);
  sel.removeAllRanges();
  sel.addRange(range);

  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }

  sel.removeAllRanges();
  document.body.removeChild(node);
  return ok;
}

async function copyOutput() {
  if (!lastText) return;

  // Preferred path: async Clipboard API (secure contexts — localhost / HTTPS).
  try {
    if (lastHtml && window.ClipboardItem && navigator.clipboard?.write) {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([lastHtml], { type: "text/html" }),
          "text/plain": new Blob([lastText], { type: "text/plain" }),
        }),
      ]);
    } else if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(lastText);
    } else {
      throw new Error("Clipboard API unavailable");
    }
    setStatus("Output copied to clipboard ✓", "ok");
    toast("Copied to clipboard ✓");
    return;
  } catch (e) {
    // Fall through to the legacy path (e.g. plain-HTTP / non-secure context).
  }

  if (legacyCopy()) {
    setStatus("Output copied to clipboard ✓", "ok");
    toast("Copied to clipboard ✓");
  } else {
    setStatus("Copy failed — select the output and copy manually.", "error");
  }
}

/* ---------- clear ---------- */
function clearAll() {
  inputEl.value = "";
  outputEl.innerHTML = "";
  manualEl.innerHTML = "";
  manualKeys = [];
  lastText = lastHtml = "";
  copyBtn.disabled = true;
  setBadge(null);
  setStatus("Waiting for paste…", "info");
  inputEl.focus();
}

/* ---------- theme ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themeBtn.querySelector(".theme-icon").textContent = theme === "dark" ? "☀️" : "🌙";
  localStorage.setItem("theme", theme);
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

/* ---------- wire up ---------- */
inputEl.addEventListener("input", () => { clearManualValues(); scheduleFormat(); });
inputEl.addEventListener("paste", () => setTimeout(() => { clearManualValues(); format(); }, 10));
copyBtn.addEventListener("click", copyOutput);
clearBtn.addEventListener("click", clearAll);
themeBtn.addEventListener("click", toggleTheme);

// Initial theme: stored preference, else OS preference.
applyTheme(
  localStorage.getItem("theme") ||
  (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light")
);
copyBtn.disabled = true;
inputEl.focus();