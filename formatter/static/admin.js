"use strict";

// Admin editor for validation rules.
// In-memory model: { isp: { atoll: [values], zones: { zone: [patterns] } } }.
// Rendered as editable cards; serialized back to /api/rules on save.

const rulesEl = document.getElementById("rules");
const saveBtn = document.getElementById("save");
const addIspBtn = document.getElementById("add-isp");
const saveStatusEl = document.getElementById("save-status");
const saveStatusTextEl = saveStatusEl.querySelector(".status-text");

let model = {};

function setSaveStatus(text, level) {
  saveStatusTextEl.textContent = text;
  saveStatusEl.className = "status " + (level || "info");
}

function ispEntry(isp) {
  // Normalize: ensure {atoll: [], zones: {}}.
  const e = model[isp] || {};
  if (!Array.isArray(e.atoll)) e.atoll = [];
  if (!e.zones || typeof e.zones !== "object") e.zones = {};
  model[isp] = e;
  return e;
}

/* ---------- rendering ---------- */
function render() {
  rulesEl.innerHTML = "";
  const isps = Object.keys(model);
  if (!isps.length) {
    rulesEl.innerHTML = `<p class="hint">No rules yet — add an ISP to get started.</p>`;
    return;
  }
  for (const isp of isps) rulesEl.append(ispCard(isp));
}

function ispCard(isp) {
  const entry = ispEntry(isp);
  const card = document.createElement("div");
  card.className = "isp-card";

  const head = document.createElement("div");
  head.className = "isp-head";
  const title = document.createElement("h3");
  title.textContent = isp;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "btn ghost sm";
  del.textContent = "Remove ISP";
  del.addEventListener("click", () => { delete model[isp]; render(); });
  head.append(title, del);
  card.append(head);

  // Atoll allowed values.
  const atollRow = document.createElement("div");
  atollRow.className = "field-row";
  const atollLbl = document.createElement("label");
  atollLbl.textContent = "Atoll (allowed values)";
  const atollInp = document.createElement("input");
  atollInp.className = "field-values";
  atollInp.value = entry.atoll.join(", ");
  atollInp.placeholder = "Kaafu";
  atollInp.addEventListener("input", () => {
    entry.atoll = atollInp.value.split(",").map((s) => s.trim()).filter(Boolean);
  });
  atollRow.append(atollLbl, atollInp);
  card.append(atollRow);

  // Zones -> address-ID patterns.
  const zonesLbl = document.createElement("label");
  zonesLbl.className = "zones-label";
  zonesLbl.textContent = "Zones (street → allowed address-ID patterns)";
  card.append(zonesLbl);

  const table = document.createElement("div");
  table.className = "zone-table";
  for (const [zone, patterns] of Object.entries(entry.zones)) {
    table.append(zoneRow(isp, zone, patterns));
  }
  card.append(table);

  const addZone = document.createElement("button");
  addZone.type = "button";
  addZone.className = "btn ghost sm";
  addZone.textContent = "+ Add zone";
  addZone.addEventListener("click", () => {
    entry.zones[uniqueZoneName(isp)] = [];
    render();
  });
  card.append(addZone);

  return card;
}

function zoneRow(isp, zone, patterns) {
  const entry = ispEntry(isp);
  const row = document.createElement("div");
  row.className = "zone-row";

  const zoneInp = document.createElement("input");
  zoneInp.className = "zone-name";
  zoneInp.value = zone;
  zoneInp.placeholder = "Zone (e.g. Phase 1)";
  zoneInp.addEventListener("change", () => {
    const newName = zoneInp.value.trim();
    if (!newName || newName === zone) return;
    const rebuilt = {};
    for (const [k, v] of Object.entries(entry.zones)) {
      rebuilt[k === zone ? newName : k] = v;
    }
    entry.zones = rebuilt;
    render();
  });

  const patInp = document.createElement("input");
  patInp.className = "zone-patterns";
  patInp.value = patterns.join(", ");
  patInp.placeholder = "TGR*, R1B*, R2B*";
  patInp.addEventListener("input", () => {
    entry.zones[zoneInp.value.trim() || zone] =
      patInp.value.split(",").map((s) => s.trim()).filter(Boolean);
  });

  const del = document.createElement("button");
  del.type = "button";
  del.className = "btn ghost sm";
  del.textContent = "✕";
  del.title = "Remove zone";
  del.addEventListener("click", () => { delete entry.zones[zone]; render(); });

  row.append(zoneInp, patInp, del);
  return row;
}

function uniqueZoneName(isp) {
  const zones = ispEntry(isp).zones;
  let i = 1;
  let name = "New zone";
  while (zones[name]) name = `New zone ${++i}`;
  return name;
}

/* ---------- actions ---------- */
addIspBtn.addEventListener("click", () => {
  const name = (prompt("ISP name (must match the detected ISP, e.g. Ooredoo):") || "").trim();
  if (!name) return;
  ispEntry(name);
  render();
});

saveBtn.addEventListener("click", async () => {
  setSaveStatus("Saving…", "info");
  try {
    const res = await fetch("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(model),
    });
    if (res.status === 401) {
      setSaveStatus("Session expired — reload and sign in again.", "error");
      return;
    }
    const data = await res.json();
    model = data.rules || {};
    render();
    setSaveStatus("Saved ✓", "ok");
  } catch (e) {
    setSaveStatus("Save failed — check the connection.", "error");
  }
});

/* ---------- init ---------- */
(async function load() {
  try {
    const res = await fetch("/api/rules");
    if (res.status === 401) { location.href = "/admin"; return; }
    model = await res.json();
  } catch (e) {
    model = {};
  }
  render();
})();