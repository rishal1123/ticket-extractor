/* Customer Performance Report - native (no-iframe) renderer + preset manager.
   Fetches the generated dataset and renders all sections with Bootstrap, with
   live prefix + date-range filters. */
(function () {
  const $ = (id) => document.getElementById(id);
  const API = '/api/reports/customer-performance';
  let DATA = { generated_at: null, prefixes: [], records: [] };
  let PRESETS = [];

  const esc = (s) => (s == null ? '' : ('' + s)).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v) => (v ? v : '');

  // ---------- filters ----------
  function activePrefixes() {
    return ($('filterPrefixes').value || '').split(',')
      .map((s) => s.trim().replace(/\*+$/, '').trim()).filter(Boolean).map((s) => s.toUpperCase());
  }
  function recMatchesPrefix(rec, prefs) {
    if (!prefs.length) return true; // empty filter = show everything loaded
    const T = (rec.t || '').toUpperCase();
    return prefs.some((p) => T.indexOf(p) !== -1);
  }
  function inDateRange(rec) {
    const from = $('dateFrom').value, to = $('dateTo').value;
    if (!from && !to) return true;
    const d = (rec.d || '').slice(0, 10);
    if (!d) return false;
    if (from && d < from) return false;
    if (to && d > to) return false;
    return true;
  }
  function groupOf(rec, prefs) {
    const list = prefs.length ? prefs : DATA.prefixes.map((p) => p.toUpperCase());
    const T = (rec.t || '').toUpperCase();
    return list.find((p) => T.indexOf(p) !== -1) || (DATA.prefixes[0] || '?').toUpperCase();
  }

  // ---------- aggregation + render ----------
  const inc2 = (map, r, c) => { if (!map.has(r)) map.set(r, new Map()); const m = map.get(r); m.set(c, (m.get(c) || 0) + 1); };

  function matrixCard(title, rowLabel, counts, colSet) {
    const cols = Array.from(colSet).sort();
    const rows = Array.from(counts.keys());
    const rt = (r) => { let s = 0; counts.get(r).forEach((v) => s += v); return s; };
    rows.sort((a, b) => rt(b) - rt(a));
    const colTot = {}; cols.forEach((c) => colTot[c] = 0); let grand = 0; let body = '';
    rows.forEach((r) => {
      const m = counts.get(r); let row = 0; let cells = '';
      cols.forEach((c) => { const v = m.get(c) || 0; colTot[c] += v; row += v; cells += `<td class="text-end">${num(v)}</td>`; });
      grand += row;
      body += `<tr><td>${esc(r)}</td>${cells}<td class="text-end fw-bold">${row}</td></tr>`;
    });
    const foot = `<tr class="table-total-row fw-bold"><td>Total</td>${cols.map((c) => `<td class="text-end">${colTot[c]}</td>`).join('')}<td class="text-end">${grand}</td></tr>`;
    const head = cols.map((c) => `<th class="text-end">${esc(c)}</th>`).join('');
    return `<div class="table-container mb-3"><h6 class="mb-2">${esc(title)}</h6>
      <div class="table-responsive" style="max-height:420px;overflow:auto">
      <table class="table table-sm table-hover mb-0"><thead class="table-light sticky-top"><tr><th>${esc(rowLabel)}</th>${head}<th class="text-end">Total</th></tr></thead>
      <tbody>${body || '<tr><td>No data.</td></tr>'}${foot}</tbody></table></div></div>`;
  }

  function recompute() {
    if (!DATA.records.length) {
      ['cTickets', 'cCustomers', 'cStaff', 'cNotes', 'cVisits'].forEach((id) => $(id).textContent = '0');
      $('reportBody').innerHTML = '<div class="text-muted text-center py-5">No report data yet. Choose a saved report (or enter prefixes) and click <b>Generate</b>.</div>';
      return;
    }
    const prefs = activePrefixes();
    const recs = DATA.records.filter((r) => recMatchesPrefix(r, prefs) && inDateRange(r));

    const customers = new Set(), users = new Map();
    const U = (n) => { if (!users.has(n)) users.set(n, { c: 0, nt: 0, t: new Set() }); return users.get(n); };
    const isp_type = new Map(), staff_isp = new Map(), staff_type = new Map();
    const itn = new Map(), sin = new Map(), stn = new Map();
    const portals = new Set(), types = new Set();
    const grpT = new Map(), grpC = new Map();
    let totalNotes = 0; const visits = []; const sv = new Map();

    recs.forEach((r) => {
      portals.add(r.p); types.add(r.ty);
      const gp = groupOf(r, prefs);
      grpT.set(gp, (grpT.get(gp) || 0) + 1);
      if (r.cid) { customers.add(r.cid); if (!grpC.has(gp)) grpC.set(gp, new Set()); grpC.get(gp).add(r.cid); }
      const cu = U(r.cr); cu.c++; cu.t.add(r.n);
      inc2(isp_type, r.p, r.ty); inc2(staff_isp, r.cr, r.p); inc2(staff_type, r.cr, r.ty);
      (r.nt || []).forEach((a) => {
        const au = U(a); au.nt++; au.t.add(r.n); totalNotes++;
        inc2(itn, r.p, r.ty); inc2(sin, a, r.p); inc2(stn, a, r.ty);
      });
      (r.v || []).forEach((v) => { visits.push({ r, v }); (v.a && v.a.length ? v.a : ['(unassigned)']).forEach((s) => sv.set(s, (sv.get(s) || 0) + 1)); });
    });

    $('cTickets').textContent = recs.length;
    $('cCustomers').textContent = customers.size;
    $('cStaff').textContent = users.size;
    $('cNotes').textContent = totalNotes;
    $('cVisits').textContent = visits.length;

    let html = '';

    // Customer group
    const glist = prefs.length ? prefs : DATA.prefixes.map((p) => p.toUpperCase());
    let grows = ''; let gT = 0, gC = 0;
    glist.forEach((p) => {
      const tk = grpT.get(p) || 0; const cs = (grpC.get(p) || new Set()).size; gT += tk; gC += cs;
      grows += `<tr><td>${esc(p)}*</td><td class="text-end">${tk}</td><td class="text-end">${cs}</td></tr>`;
    });
    grows += `<tr class="table-total-row fw-bold"><td>Total</td><td class="text-end">${gT}</td><td class="text-end">${gC}</td></tr>`;
    html += `<div class="table-container mb-3"><h6 class="mb-2"><i class="bi bi-people me-1"></i>Tickets by Customer Group</h6>
      <div class="table-responsive"><table class="table table-sm mb-0" style="max-width:520px"><thead class="table-light"><tr><th>Group</th><th class="text-end">Tickets</th><th class="text-end">Customers</th></tr></thead><tbody>${grows}</tbody></table></div></div>`;

    // User performance (Total = Tickets Created + Notes Written)
    const ul = Array.from(users.entries()).map((e) => ({ n: e[0], o: e[1] }));
    ul.sort((a, b) => (b.o.c + b.o.nt) - (a.o.c + a.o.nt) || b.o.c - a.o.c);
    const urows = ul.map((u) => `<tr><td>${esc(u.n)}</td><td class="text-end">${u.o.c}</td><td class="text-end">${u.o.nt}</td><td class="text-end fw-bold">${u.o.c + u.o.nt}</td></tr>`).join('')
      || '<tr><td colspan="4" class="text-muted">No users.</td></tr>';
    html += `<div class="table-container mb-3"><h6 class="mb-2"><i class="bi bi-person-badge me-1"></i>User Performance — Ticket &amp; Note Volume</h6>
      <div class="table-responsive" style="max-height:420px;overflow:auto"><table class="table table-sm table-hover mb-0"><thead class="table-light sticky-top"><tr><th>User</th><th class="text-end">Tickets Created</th><th class="text-end">Notes Written</th><th class="text-end" title="Tickets Created + Notes Written">Total</th></tr></thead><tbody>${urows}</tbody></table></div></div>`;

    // Matrices
    html += '<div class="row g-3"><div class="col-12 col-xl-6">'
      + matrixCard('Tickets Created by ISP and Type', 'ISP', isp_type, types)
      + matrixCard('Tickets Created by Staff and ISP', 'Staff', staff_isp, portals)
      + matrixCard('Tickets Created by Staff and Type', 'Staff', staff_type, types)
      + '</div><div class="col-12 col-xl-6">'
      + matrixCard('Notes Written by ISP and Type', 'ISP', itn, types)
      + matrixCard('Notes Written by Staff and ISP', 'Staff', sin, portals)
      + matrixCard('Notes Written by Staff and Type', 'Staff', stn, types)
      + '</div></div>';

    // Site visits by staff
    const svArr = Array.from(sv.entries()).sort((a, b) => b[1] - a[1]);
    const svrows = svArr.map((e) => `<tr><td>${esc(e[0])}</td><td class="text-end">${e[1]}</td></tr>`).join('')
      || '<tr><td colspan="2" class="text-muted">No site visits.</td></tr>';
    html += `<div class="table-container mb-3"><h6 class="mb-2"><i class="bi bi-geo-alt me-1"></i>Site Visits Attended by Staff</h6>
      <div class="table-responsive"><table class="table table-sm mb-0" style="max-width:480px"><thead class="table-light"><tr><th>Assigned Staff</th><th class="text-end">Site Visits</th></tr></thead><tbody>${svrows}</tbody></table></div></div>`;

    // Site visit detail
    visits.sort((x, y) => (y.v.dt || '').localeCompare(x.v.dt || ''));
    const vrows = visits.map((o) => {
      const r = o.r, v = o.v;
      return `<tr><td><a href="${esc(r.u)}" target="_blank">${esc(r.n)}</a></td><td>${esc(v.dt)}</td><td>${esc(v.tm)}</td><td>${esc((v.a || []).join(', ') || '(unassigned)')}</td><td>${esc(r.p)}</td><td>${esc(r.ty)}</td><td>${esc(v.s)}</td><td>${esc(v.c)}</td><td>${esc(v.ad)}</td></tr>`;
    }).join('') || '<tr><td colspan="9" class="text-muted">No site visits.</td></tr>';
    html += `<div class="table-container mb-3"><h6 class="mb-2"><i class="bi bi-geo me-1"></i>Site Visits (${visits.length})</h6>
      <div class="table-responsive" style="max-height:460px;overflow:auto"><table class="table table-sm table-hover mb-0"><thead class="table-light sticky-top"><tr><th>Ticket #</th><th>Date</th><th>Time</th><th>Assigned</th><th>ISP</th><th>Type</th><th>Site Type</th><th>Customer</th><th>Address</th></tr></thead><tbody>${vrows}</tbody></table></div></div>`;

    // Tickets
    const tl = recs.slice().sort((a, b) => (b.d || '').localeCompare(a.d || ''));
    const trows = tl.map((r) => `<tr><td><a href="${esc(r.u)}" target="_blank">${esc(r.n)}</a></td><td>${esc(r.cid)}</td><td>${esc(r.p)}</td><td>${esc(r.ty)}</td><td>${esc(r.t)}</td><td>${esc(r.cr)}</td><td>${esc(r.st)}</td><td class="text-end">${r.ac}</td><td>${esc(r.d)}</td></tr>`).join('')
      || '<tr><td colspan="9" class="text-muted">No tickets.</td></tr>';
    html += `<div class="table-container mb-3"><h6 class="mb-2"><i class="bi bi-list-ul me-1"></i>Tickets (${recs.length})</h6>
      <div class="table-responsive" style="max-height:520px;overflow:auto"><table class="table table-sm table-hover mb-0"><thead class="table-light sticky-top"><tr><th>Ticket #</th><th>Customer ID</th><th>ISP</th><th>Type</th><th>Title</th><th>Created By</th><th>State</th><th class="text-end">Articles</th><th>Created</th></tr></thead><tbody>${trows}</tbody></table></div></div>`;

    $('reportBody').innerHTML = html;
  }

  // ---------- data + status ----------
  function renderStatus(s) {
    const el = $('genStatus'), btn = $('genBtn');
    if (s.generating) { el.textContent = 'Generating… this can take 1–2 minutes'; btn.disabled = true; setTimeout(poll, 4000); return; }
    btn.disabled = false;
    if (s.generated_at) {
      const prefs = (s.prefixes || []).map((p) => p + '*').join(', ');
      let t = `Last generated ${new Date(s.generated_at).toLocaleString('en-GB')} (${prefs})`;
      if (s.last_result) t += ` · ${s.last_result.tickets} tickets, ${s.last_result.customers} customers, ${s.last_result.site_visits} site visits`;
      el.textContent = t;
    } else { el.textContent = s.last_error ? ('Error: ' + s.last_error) : 'Not generated yet'; }
  }
  function loadStatus(prefill) {
    return fetch(API + '/status').then((r) => r.json()).then((s) => {
      renderStatus(s);
      if (prefill && s.prefixes && !$('genPrefixes').value) $('genPrefixes').value = s.prefixes.join(', ');
    }).catch(() => {});
  }
  function loadData() {
    return fetch(API + '/data').then((r) => r.json()).then((d) => {
      DATA = d || DATA; recompute();
    }).catch(() => {});
  }
  function poll() {
    fetch(API + '/status').then((r) => r.json()).then((s) => {
      if (s.generating) { renderStatus(s); }
      else { renderStatus(s); loadData(); }
    }).catch(() => { $('genBtn').disabled = false; });
  }

  // ---------- generate ----------
  window.generateNow = function () {
    const btn = $('genBtn'); btn.disabled = true; $('genStatus').textContent = 'Starting…';
    const raw = ($('genPrefixes').value || '').trim();
    const prefixes = raw ? raw.split(',').map((s) => s.trim()).filter(Boolean) : null;
    fetch(API + '/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prefixes }) })
      .then(() => setTimeout(poll, 1500))
      .catch(() => { btn.disabled = false; });
  };

  // ---------- presets ----------
  function renderPresets() {
    const sel = $('presetSelect');
    sel.innerHTML = '<option value="">— Custom (type prefixes) —</option>'
      + PRESETS.map((p) => `<option value="${esc(p.name)}">${esc(p.name)} (${esc(p.prefixes.join(', '))})</option>`).join('');
  }
  function loadPresets() {
    return fetch(API + '/presets').then((r) => r.json()).then((d) => { PRESETS = d.presets || []; renderPresets(); }).catch(() => {});
  }
  window.onPresetChange = function () {
    const name = $('presetSelect').value;
    const p = PRESETS.find((x) => x.name === name);
    if (!p) return;
    $('genPrefixes').value = p.prefixes.join(', ');
    $('presetName').value = p.name;
    if (p.date_from !== undefined) $('dateFrom').value = p.date_from || '';
    if (p.date_to !== undefined) $('dateTo').value = p.date_to || '';
    recompute();
  };
  window.savePreset = function () {
    const name = ($('presetName').value || '').trim();
    if (!name) { alert('Enter a preset name first.'); return; }
    const prefixes = ($('genPrefixes').value || '').split(',').map((s) => s.trim()).filter(Boolean);
    if (!prefixes.length) { alert('Enter at least one prefix.'); return; }
    fetch(API + '/presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, prefixes, date_from: $('dateFrom').value, date_to: $('dateTo').value })
    }).then((r) => r.json()).then((d) => { PRESETS = d.presets || []; renderPresets(); $('presetSelect').value = name; });
  };
  window.deletePreset = function () {
    const name = $('presetSelect').value || ($('presetName').value || '').trim();
    if (!name) { alert('Select or name a preset to delete.'); return; }
    if (!confirm('Delete preset "' + name + '"?')) return;
    fetch(API + '/presets/' + encodeURIComponent(name), { method: 'DELETE' })
      .then((r) => r.json()).then((d) => { PRESETS = d.presets || []; renderPresets(); $('presetName').value = ''; });
  };
  window.resetFilters = function () {
    $('filterPrefixes').value = ''; $('dateFrom').value = ''; $('dateTo').value = ''; recompute();
  };

  document.addEventListener('DOMContentLoaded', function () {
    $('filterPrefixes').addEventListener('input', recompute);
    $('dateFrom').addEventListener('change', recompute);
    $('dateTo').addEventListener('change', recompute);
    loadPresets();
    loadStatus(true);
    loadData();
  });
})();
