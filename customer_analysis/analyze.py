"""
Standalone customer-performance analyzer (separate from the dashboard app).

Searches Znuny for every ticket whose CustomerID starts with one of the given
prefixes (default: UD-, DH-), pulls all of those tickets together with every
article/note, then produces a per-user "ticket & note volume" performance
report as a self-contained HTML file.

This script only *reads* from Znuny via the existing REST client. It does not
touch the dashboard, the scheduler, or the SQLite database.

Usage (from the project root):

    python -m customer_analysis.analyze
    python -m customer_analysis.analyze --prefixes UD,DH --out customer_analysis/report.html
    python customer_analysis/analyze.py --prefixes UD

Credentials are read the same way the main app reads them (DB app_settings ->
.env fallback), via config.Config / ZnunyClient.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

# Allow running as a loose script (python customer_analysis/analyze.py) as well
# as a module (python -m customer_analysis.analyze).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from znuny_client import ZnunyClient, _parse_dt, _parse_from_name  # noqa: E402

# How many ticket ids to pull per batched TicketGet (matches client batching).
BATCH_SIZE = 20


@dataclass
class TicketRow:
    number: str
    title: str
    customer_id: str
    portal: str
    ticket_type: str
    state: str
    queue: str
    creator: str
    created_at: datetime | None
    article_count: int
    url: str


@dataclass
class UserStats:
    name: str
    tickets_created: int = 0
    notes_written: int = 0
    tickets_touched: set = field(default_factory=set)


def search_ticket_ids(client: ZnunyClient, prefixes: list[str], log=print) -> list[str]:
    """Return de-duplicated TicketIDs whose Title contains any prefix* code.

    The title is the authoritative signal: a ticket only counts if its title
    carries a UD-/DH- customer code (the CustomerID field alone is not enough).
    Runs one TicketSearch per prefix on the Title field and merges the results.
    No StateType is passed, so both open and closed tickets are returned.
    """
    seen: dict[str, None] = {}
    for prefix in prefixes:
        title_ids = client._ticket_search(Title=f"*{prefix}*", Limit=10000)
        log(f"  Title *{prefix}*: {len(title_ids)} tickets")
        for tid in title_ids:
            seen.setdefault(tid, None)
    return list(seen.keys())


def fetch_tickets(client: ZnunyClient, ticket_ids: list[str], log=print) -> list[dict]:
    """Batched TicketGet (with articles) for all ids."""
    tickets: list[dict] = []
    total = len(ticket_ids)
    for i in range(0, total, BATCH_SIZE):
        chunk = ticket_ids[i:i + BATCH_SIZE]
        batch = client._ticket_get(chunk, with_articles=True)
        tickets.extend(batch)
        log(f"  Fetched {min(i + BATCH_SIZE, total)}/{total} tickets")
    return tickets


def matches_prefix(title: str, prefixes: list[str]) -> bool:
    """True if a UD-/DH- prefix code appears anywhere in the ticket Title."""
    ttl = (title or "").upper()
    return any(p.upper() in ttl for p in prefixes)


def extract_code(title: str, prefixes: list[str]) -> str:
    """Pull the first UD-/DH- style customer code out of a title (e.g. UD-01-02-01)."""
    for p in prefixes:
        m = re.search(rf"\b{re.escape(p)}[\w-]*", title, re.IGNORECASE)
        if m:
            return m.group(0).upper()
    return ""


KNOWN_PORTALS = ("Dhiraagu", "Ooredoo", "ROL", "Medianet")


def parse_portal_type(title: str, prefixes: list[str]) -> tuple[str, str]:
    """Extract (ISP, ticket type) from a title like 'Ooredoo - New Service - UD-... / ...'.

    Standard service tickets follow '{ISP} - {Type} - {UD/DH code} / ...'. Site-visit
    style tickets ('Dhiraagu OAN Site Visit Arranged - UD-...') don't carry a clean
    type segment, so they are bucketed into Site Visit / Preventative Maintenance /
    Other rather than producing noisy one-off "types".
    """
    # ISP: match a known portal name anywhere in the title (case-insensitive).
    portal = "Unknown"
    for p in KNOWN_PORTALS:
        if re.search(rf"\b{p}\b", title, re.IGNORECASE):
            portal = p
            break

    # Standard format: a *bare* ISP name, then ' - {Type} - {code}'. Requiring the
    # bare ISP before ' - ' keeps "Dhiraagu OAN Site Visit ..." out of this branch.
    isp_alt = "|".join(KNOWN_PORTALS)
    code_alt = "|".join(re.escape(p) for p in prefixes)
    m = re.match(rf"\s*(?:{isp_alt})\s*-\s*(.+?)\s*-\s*(?:{code_alt})", title, re.IGNORECASE)
    if m:
        return portal, m.group(1).strip()

    # Non-standard titles -> clean buckets.
    low = title.lower()
    if "site visit" in low:
        return portal, "Site Visit"
    if "preventative maintenance" in low:
        return portal, "Preventative Maintenance"
    return portal, "Other"


@dataclass
class SiteVisitRow:
    ticket_number: str
    url: str
    portal: str
    ticket_type: str
    assigned: list           # list of staff names parsed from "Assigned to:"
    site_type: str
    scheduled_time: str
    visit_date: str
    customer_name: str
    address: str


def _field(body: str, label: str) -> str:
    """Pull a 'Label: value' line out of a site-visit article body."""
    m = re.search(rf"{label}\s*:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_arranged_visit(subject: str, body: str, created_at) -> dict | None:
    """Parse a 'Site Visit Arranged' article into visit fields, or None.

    Detects both 'OAN Site Visit Arranged' / plain 'Site Visit Arranged' and
    'Preventative Maintenance - Site Visit' articles, then reads the structured
    body (Address / Customer Name / Site Type / Time / Assigned to: @staff ...).
    """
    s = (subject or "").lower()
    b = (body or "")
    is_visit = (
        "site visit arranged" in s
        or "site visit arranged" in b.lower()
        or "preventative maintenance - site visit" in s
    )
    if not is_visit:
        return None

    # Assigned staff. Znuny renders @mentions with a link marker, so the raw value
    # looks like "[1]@razwan" or "[1]@aslan [2]@ayan"; combos use "&"/"and"/",".
    raw_assigned = _field(b, "Assigned to")
    raw_assigned = re.sub(r"\[\d+\]", " ", raw_assigned)            # drop [1], [2] markers
    parts = re.split(r"\s*@\s*|\s*,\s*|\s*&\s*|\s+and\s+", raw_assigned, flags=re.IGNORECASE)
    assigned = []
    for n in parts:
        n = n.strip().lstrip("@").strip()
        if n:
            assigned.append(n.lower().title())                     # normalize case for grouping

    site_type = _field(b, "(?:Site\\s*)?Type")
    scheduled_time = _field(b, "Time")
    if scheduled_time.lower() in ("now", "nnow") and created_at:
        scheduled_time = created_at.strftime("%H:%M")
    return {
        "assigned": assigned,
        "site_type": site_type,
        "scheduled_time": scheduled_time,
        "visit_date": created_at.strftime("%Y-%m-%d") if created_at else "",
        "customer_name": _field(b, "Customer Name"),
        "address": _field(b, "Address"),
    }


def analyze(tickets: list[dict], client: ZnunyClient, prefixes: list[str]):
    """Build ticket rows and per-user stats from raw Znuny ticket dicts."""
    # Harvest agent display names across ALL tickets first, so a creator who only
    # appears in another ticket's article still resolves to a name.
    for t in tickets:
        client._harvest_user_names(t)

    rows: list[TicketRow] = []
    users: dict[str, UserStats] = {}
    customers: set[str] = set()

    # Cross-tabs based on ticket creation (attributed to the ticket creator).
    isp_type = defaultdict(lambda: defaultdict(int))   # isp_type[portal][type] = count
    staff_isp = defaultdict(lambda: defaultdict(int))  # staff_isp[staff][portal] = count
    staff_type = defaultdict(lambda: defaultdict(int))  # staff_type[staff][type] = count
    # Cross-tabs based on notes written (each agent article -> its author, using the
    # parent ticket's ISP/type).
    isp_type_notes = defaultdict(lambda: defaultdict(int))
    staff_isp_notes = defaultdict(lambda: defaultdict(int))
    staff_type_notes = defaultdict(lambda: defaultdict(int))
    all_portals: set[str] = set()
    all_types: set[str] = set()
    # Per customer-group (UD-/DH-) totals.
    prefix_tickets = defaultdict(int)              # prefix -> ticket count
    prefix_customers = defaultdict(set)            # prefix -> set of customer codes
    # Site Visits Arranged: per-visit rows + tally of visits attended per staff.
    site_visits: list[SiteVisitRow] = []
    sv_by_staff = defaultdict(int)                 # staff -> site visits attended
    # Per-ticket records embedded in the HTML for client-side filtering.
    records: list[dict] = []

    def user(name: str) -> UserStats:
        key = name or "(unknown)"
        if key not in users:
            users[key] = UserStats(name=key)
        return users[key]

    for t in tickets:
        customer_id = t.get("CustomerID") or ""
        title = t.get("Title") or ""
        # The title is authoritative: keep only tickets whose title carries a
        # UD-/DH- code (a matching CustomerID alone is not enough).
        if not matches_prefix(title, prefixes):
            continue
        # Attribute the ticket to its customer group (the prefix matched in title).
        matched_prefix = next(
            (p for p in prefixes if p.upper() in title.upper()), prefixes[0]
        )
        prefix_tickets[matched_prefix] += 1
        # Identify the customer by the CustomerID field (the stable account id used
        # to compare against the full customer base). Fall back to the title code.
        cid = (customer_id or "").strip().upper()
        cust_prefix = next((p for p in prefixes if cid.startswith(p.upper())), None)
        if not cust_prefix:
            cid = extract_code(title, prefixes)
            cust_prefix = next((p for p in prefixes if cid.startswith(p.upper())), matched_prefix)
        if cid:
            customers.add(cid)
            prefix_customers[cust_prefix].add(cid)

        portal, ticket_type = parse_portal_type(title, prefixes)
        all_portals.add(portal)
        all_types.add(ticket_type)

        number = str(t.get("TicketNumber", ""))
        tid = str(t.get("TicketID", ""))
        creator = client._name_for(t.get("CreateBy"), fallback=t.get("Owner") or "")

        raw_articles = t.get("Article") or []
        rows.append(TicketRow(
            number=number,
            title=title,
            customer_id=customer_id,
            portal=portal,
            ticket_type=ticket_type,
            state=t.get("State") or "",
            queue=t.get("Queue") or "",
            creator=creator,
            created_at=_parse_dt(t.get("Created") or ""),
            article_count=len(raw_articles),
            url=client._zoom_url(tid),
        ))

        # Ticket creation counts toward the creator.
        cu = user(creator)
        cu.tickets_created += 1
        cu.tickets_touched.add(number)

        # Cross-tab counts (per ISP and per type, attributed to the creator).
        isp_type[portal][ticket_type] += 1
        staff_isp[creator or "(unknown)"][portal] += 1
        staff_type[creator or "(unknown)"][ticket_type] += 1

        # Each agent-authored article is a note written by that staff member,
        # attributed to the parent ticket's ISP and type.
        note_authors: list[str] = []
        for a in raw_articles:
            if str(a.get("SenderType")) != "agent":
                continue
            author = _parse_from_name(a.get("From") or "") or client._name_for(a.get("CreateBy"), "")
            au = user(author)
            au.notes_written += 1
            au.tickets_touched.add(number)
            isp_type_notes[portal][ticket_type] += 1
            staff_isp_notes[author or "(unknown)"][portal] += 1
            staff_type_notes[author or "(unknown)"][ticket_type] += 1
            note_authors.append(author or "(unknown)")

        # Site Visits Arranged: scan every article (any sender) for the visit format.
        ticket_visits: list[dict] = []
        for a in raw_articles:
            visit = parse_arranged_visit(a.get("Subject") or "", a.get("Body") or "",
                                         _parse_dt(a.get("CreateTime") or ""))
            if not visit:
                continue
            site_visits.append(SiteVisitRow(
                ticket_number=number,
                url=client._zoom_url(tid),
                portal=portal,
                ticket_type=ticket_type,
                assigned=visit["assigned"],
                site_type=visit["site_type"],
                scheduled_time=visit["scheduled_time"],
                visit_date=visit["visit_date"],
                customer_name=visit["customer_name"],
                address=visit["address"],
            ))
            for staff in (visit["assigned"] or ["(unassigned)"]):
                sv_by_staff[staff] += 1
            ticket_visits.append({
                "a": visit["assigned"] or ["(unassigned)"],
                "s": visit["site_type"],
                "tm": visit["scheduled_time"],
                "dt": visit["visit_date"],
                "c": visit["customer_name"],
                "ad": visit["address"],
            })

        # Per-ticket record embedded in the report for live, in-browser filtering.
        records.append({
            "n": number,
            "u": client._zoom_url(tid),
            "cid": cid,
            "p": portal,
            "ty": ticket_type,
            "cr": creator or "(unknown)",
            "st": t.get("State") or "",
            "t": title,
            "d": rows[-1].created_at.strftime("%Y-%m-%d %H:%M") if rows[-1].created_at else "",
            "ac": len(raw_articles),
            "nt": note_authors,
            "v": ticket_visits,
        })

    stats = {
        "isp_type": isp_type,
        "staff_isp": staff_isp,
        "staff_type": staff_type,
        "isp_type_notes": isp_type_notes,
        "staff_isp_notes": staff_isp_notes,
        "staff_type_notes": staff_type_notes,
        "portals": sorted(all_portals),
        "types": sorted(all_types),
        "prefix_tickets": prefix_tickets,
        "prefix_customers": {p: len(c) for p, c in prefix_customers.items()},
        "site_visits": site_visits,
        "sv_by_staff": dict(sv_by_staff),
        "records": records,
    }
    return rows, users, customers, stats


def render_html(rows, users, customers, stats, prefixes, out_path):
    """Render the self-contained HTML report."""
    e = html.escape
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # All aggregation happens client-side from these per-ticket records, so the
    # in-page prefix filter can recompute every table live.
    records = stats.get("records", [])
    data_json = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")
    prefixes_json = json.dumps(prefixes)
    filter_default = ", ".join(prefixes)

    page = _PAGE_TEMPLATE
    page = page.replace("__DATA__", data_json)
    page = page.replace("__PREFIXES__", prefixes_json)
    page = page.replace("__FILTER_DEFAULT__", e(filter_default))
    page = page.replace("__GENERATED__", e(now))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)


_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Customer Performance Report</title>
<style>
  :root { --primary:#2c3e50; --accent:#3498db; --line:#e1e4e8; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
          margin:0; padding:24px; background:#f5f6f8; color:#222; }
  h1 { margin:0 0 4px; font-size:22px; color:var(--primary); }
  h2 { margin:32px 0 12px; font-size:18px; color:var(--primary);
        border-bottom:2px solid var(--accent); padding-bottom:6px; }
  .meta { color:#666; font-size:13px; margin-bottom:16px; }
  .filterbar { background:#fff; border:1px solid var(--line); border-radius:8px;
               padding:14px 18px; margin-bottom:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
  .filterbar label { font-weight:600; color:var(--primary); font-size:14px; margin-right:8px; }
  .filterbar input { font-size:14px; padding:8px 10px; border:1px solid var(--line); border-radius:6px; }
  .filterbar input[type=text] { width:340px; max-width:100%; }
  .filterbar input[type=date] { width:auto; }
  .filterbar .row2 { margin-top:10px; }
  .filterbar button { font-size:13px; padding:8px 12px; margin-left:8px; cursor:pointer;
                      border:1px solid var(--line); border-radius:6px; background:#f1f3f5; }
  .filterbar .hint { display:block; color:#888; font-size:12px; margin-top:8px; }
  .cards { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:8px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:8px;
           padding:16px 20px; min-width:150px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
  .card .label { font-size:12px; color:#777; text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:28px; font-weight:700; color:var(--accent); }
  table { width:100%; border-collapse:collapse; background:#fff;
           border:1px solid var(--line); border-radius:8px; overflow:hidden; font-size:14px; }
  th, td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); }
  th { background:var(--primary); color:#fff; font-weight:600; position:sticky; top:0; }
  tr:nth-child(even) td { background:#fafbfc; }
  th.num, td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .total { font-weight:700; background:#eef2f6 !important; }
  ul.legend { list-style:none; padding:0; margin:0 0 14px; font-size:13px; color:#555;
               display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:4px 20px; }
  ul.legend strong { color:var(--primary); }
  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .scroll { max-height:600px; overflow:auto; border-radius:8px; }
</style>
</head>
<body>
  <h1>Customer Performance Report</h1>
  <div class="meta">Generated __GENERATED__</div>

  <div class="filterbar">
    <label for="filterInput">Show customer groups:</label>
    <input id="filterInput" type="text" value="__FILTER_DEFAULT__" autocomplete="off">
    <button id="resetBtn" type="button">Reset</button>
    <div class="row2">
      <label for="dateFrom">Created between:</label>
      <input id="dateFrom" type="date">
      <label for="dateTo" style="margin:0 8px">and</label>
      <input id="dateTo" type="date">
    </div>
    <span class="hint">Prefixes are comma-separated, trailing <code>*</code> optional
      (e.g. <code>UD-16-*, UD-15-*, MPL-*</code>). The date range filters tickets by their
      created date. The whole report recomputes as you change either filter.</span>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Tickets</div><div class="value" id="cTickets">0</div></div>
    <div class="card"><div class="label">Customers</div><div class="value" id="cCustomers">0</div></div>
    <div class="card"><div class="label">Staff Users</div><div class="value" id="cStaff">0</div></div>
    <div class="card"><div class="label">Notes Written</div><div class="value" id="cNotes">0</div></div>
    <div class="card"><div class="label">Site Visits</div><div class="value" id="cVisits">0</div></div>
  </div>

  <h2>Tickets by Customer Group</h2>
  <ul class="legend">
    <li><strong>Tickets</strong> &mdash; qualifying tickets (title contains the code) in this group.</li>
    <li><strong>Customers</strong> &mdash; distinct customers (CustomerID) that have a qualifying ticket.</li>
  </ul>
  <table style="max-width:480px">
    <thead><tr><th>Customer Group</th><th class="num">Tickets</th><th class="num">Customers</th></tr></thead>
    <tbody id="grpBody"></tbody>
  </table>

  <h2>User Performance &mdash; Ticket &amp; Note Volume</h2>
  <ul class="legend">
    <li><strong>Tickets Created</strong> &mdash; tickets where this user is the original creator.</li>
    <li><strong>Notes Written</strong> &mdash; total agent articles/notes authored by this user.</li>
    <li><strong>Total</strong> &mdash; Tickets Created + Notes Written (overall activity for the staff member).</li>
  </ul>
  <table>
    <thead><tr>
      <th>User</th><th class="num">Tickets Created</th>
      <th class="num">Notes Written</th><th class="num">Total</th>
    </tr></thead>
    <tbody id="userBody"></tbody>
  </table>

  <div id="matrices"></div>

  <h2>Site Visits Attended by Staff</h2>
  <ul class="legend">
    <li>Counts every "Site Visit Arranged" article, attributed to each name on its
        <strong>Assigned to:</strong> line. A visit assigned to multiple staff counts for each.</li>
  </ul>
  <table style="max-width:480px">
    <thead><tr><th>Assigned Staff</th><th class="num">Site Visits Attended</th></tr></thead>
    <tbody id="svStaffBody"></tbody>
  </table>

  <h2>Site Visits (<span id="svCount">0</span>)</h2>
  <div class="scroll">
  <table>
    <thead><tr>
      <th>Ticket #</th><th>Date</th><th>Time</th><th>Assigned</th><th>ISP</th>
      <th>Type</th><th>Site Type</th><th>Customer</th><th>Address</th>
    </tr></thead>
    <tbody id="svDetailBody"></tbody>
  </table>
  </div>

  <h2>Tickets (<span id="tkCount">0</span>)</h2>
  <div class="scroll">
  <table>
    <thead><tr>
      <th>Ticket #</th><th>Customer ID</th><th>ISP</th><th>Type</th><th>Title</th><th>Created By</th>
      <th>State</th><th class="num">Articles</th><th>Created</th>
    </tr></thead>
    <tbody id="ticketsBody"></tbody>
  </table>
  </div>

<script>
const RECORDS = __DATA__;
const DEFAULT_PREFIXES = __PREFIXES__;

function esc(s){ s = (s==null?'':''+s); return s.replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function num(v){ return v ? v : ''; }

function parsePrefixes(){
  let ps = document.getElementById('filterInput').value
    .split(',').map(s=>s.trim().replace(/\*+$/,'').trim()).filter(Boolean);
  if(!ps.length) ps = DEFAULT_PREFIXES.slice();
  return ps.map(p=>p.toUpperCase());
}
function recPrefix(rec, prefs){
  const T = (rec.t||'').toUpperCase();
  return prefs.find(p => T.indexOf(p) !== -1) || null;
}
function inDateRange(rec){
  const from = document.getElementById('dateFrom').value;
  const to = document.getElementById('dateTo').value;
  if(!from && !to) return true;
  const d = (rec.d||'').slice(0,10);   // "YYYY-MM-DD" from "YYYY-MM-DD HH:MM"
  if(!d) return false;                 // no date -> excluded when a range is set
  if(from && d < from) return false;
  if(to && d > to) return false;
  return true;
}
function inc2(map, r, c){ if(!map.has(r)) map.set(r,new Map());
  const m=map.get(r); m.set(c,(m.get(c)||0)+1); }

function matrixHTML(title, rowLabel, counts, colSet){
  const cols = Array.from(colSet).sort();
  const rows = Array.from(counts.keys());
  const rowTotal = r => { let s=0; counts.get(r).forEach(v=>s+=v); return s; };
  rows.sort((a,b)=>rowTotal(b)-rowTotal(a));
  const colTot = {}; cols.forEach(c=>colTot[c]=0); let grand=0; let body='';
  rows.forEach(r=>{
    const m=counts.get(r); let rt=0; let cells='';
    cols.forEach(c=>{ const v=m.get(c)||0; colTot[c]+=v; rt+=v; cells+="<td class='num'>"+num(v)+"</td>"; });
    grand+=rt;
    body += "<tr><td>"+esc(r)+"</td>"+cells+"<td class='num total'>"+rt+"</td></tr>";
  });
  const foot = "<tr><td class='total'>Total</td>" +
    cols.map(c=>"<td class='num total'>"+colTot[c]+"</td>").join('') +
    "<td class='num total'>"+grand+"</td></tr>";
  const head = cols.map(c=>"<th class='num'>"+esc(c)+"</th>").join('');
  return "<h2>"+esc(title)+"</h2><div class='scroll'><table><thead><tr><th>"+esc(rowLabel)+"</th>"+
    head+"<th class='num'>Total</th></tr></thead><tbody>"+(body||"<tr><td>No data.</td></tr>")+foot+
    "</tbody></table></div>";
}

function recompute(){
  const prefs = parsePrefixes();
  const recs = RECORDS.filter(r => recPrefix(r, prefs) !== null && inDateRange(r));

  const customers = new Set();
  const users = new Map();
  const U = n => { if(!users.has(n)) users.set(n,{created:0,notes:0,touched:new Set()}); return users.get(n); };
  const isp_type=new Map(), staff_isp=new Map(), staff_type=new Map();
  const isp_type_notes=new Map(), staff_isp_notes=new Map(), staff_type_notes=new Map();
  const portals=new Set(), types=new Set();
  const grpTickets=new Map(), grpCustomers=new Map();
  prefs.forEach(p=>{ grpTickets.set(p,0); grpCustomers.set(p,new Set()); });
  let totalNotes=0; const visits=[]; const svByStaff=new Map();

  recs.forEach(r=>{
    portals.add(r.p); types.add(r.ty);
    const gp = recPrefix(r, prefs);
    grpTickets.set(gp, (grpTickets.get(gp)||0)+1);
    if(r.cid){ customers.add(r.cid);
      if(!grpCustomers.has(gp)) grpCustomers.set(gp,new Set());
      grpCustomers.get(gp).add(r.cid); }

    const cu=U(r.cr); cu.created++; cu.touched.add(r.n);
    inc2(isp_type, r.p, r.ty); inc2(staff_isp, r.cr, r.p); inc2(staff_type, r.cr, r.ty);

    (r.nt||[]).forEach(a=>{ const au=U(a); au.notes++; au.touched.add(r.n); totalNotes++;
      inc2(isp_type_notes, r.p, r.ty); inc2(staff_isp_notes, a, r.p); inc2(staff_type_notes, a, r.ty); });

    (r.v||[]).forEach(v=>{ visits.push({rec:r, v:v});
      (v.a&&v.a.length?v.a:['(unassigned)']).forEach(s=>svByStaff.set(s,(svByStaff.get(s)||0)+1)); });
  });

  document.getElementById('cTickets').textContent = recs.length;
  document.getElementById('cCustomers').textContent = customers.size;
  document.getElementById('cStaff').textContent = users.size;
  document.getElementById('cNotes').textContent = totalNotes;
  document.getElementById('cVisits').textContent = visits.length;

  let grpRows=''; let gT=0, gC=0;
  prefs.forEach(p=>{ const tk=grpTickets.get(p)||0; const cs=(grpCustomers.get(p)||new Set()).size;
    gT+=tk; gC+=cs;
    grpRows += "<tr><td>"+esc(p)+"*</td><td class='num'>"+tk+"</td><td class='num'>"+cs+"</td></tr>"; });
  grpRows += "<tr><td class='total'>Total</td><td class='num total'>"+gT+"</td><td class='num total'>"+gC+"</td></tr>";
  document.getElementById('grpBody').innerHTML = grpRows;

  const ulist = Array.from(users.entries()).map(e=>({n:e[0], o:e[1]}));
  ulist.sort((a,b)=> (b.o.created+b.o.notes)-(a.o.created+a.o.notes) || b.o.created-a.o.created);
  document.getElementById('userBody').innerHTML = ulist.map(u=>
    "<tr><td>"+esc(u.n)+"</td><td class='num'>"+u.o.created+"</td><td class='num'>"+u.o.notes+
    "</td><td class='num total'>"+(u.o.created+u.o.notes)+"</td></tr>").join('')
    || "<tr><td colspan='4'>No users found.</td></tr>";

  document.getElementById('matrices').innerHTML =
    matrixHTML('Tickets Created by ISP and Type','ISP', isp_type, types) +
    matrixHTML('Tickets Created by Staff and ISP','Staff', staff_isp, portals) +
    matrixHTML('Tickets Created by Staff and Type','Staff', staff_type, types) +
    matrixHTML('Notes Written by ISP and Type','ISP', isp_type_notes, types) +
    matrixHTML('Notes Written by Staff and ISP','Staff', staff_isp_notes, portals) +
    matrixHTML('Notes Written by Staff and Type','Staff', staff_type_notes, types);

  const svArr = Array.from(svByStaff.entries()).sort((a,b)=>b[1]-a[1]);
  document.getElementById('svStaffBody').innerHTML = svArr.map(e=>
    "<tr><td>"+esc(e[0])+"</td><td class='num'>"+e[1]+"</td></tr>").join('')
    || "<tr><td colspan='2'>No site visits found.</td></tr>";

  visits.sort((x,y)=> (y.v.dt||'').localeCompare(x.v.dt||''));
  document.getElementById('svDetailBody').innerHTML = visits.map(o=>{
    const r=o.rec, v=o.v;
    return "<tr><td><a href='"+esc(r.u)+"' target='_blank'>"+esc(r.n)+"</a></td><td>"+esc(v.dt)+
      "</td><td>"+esc(v.tm)+"</td><td>"+esc((v.a||[]).join(', ')||'(unassigned)')+"</td><td>"+esc(r.p)+
      "</td><td>"+esc(r.ty)+"</td><td>"+esc(v.s)+"</td><td>"+esc(v.c)+"</td><td>"+esc(v.ad)+"</td></tr>";
  }).join('') || "<tr><td colspan='9'>No site visits found.</td></tr>";
  document.getElementById('svCount').textContent = visits.length;

  const tlist = recs.slice().sort((a,b)=> (b.d||'').localeCompare(a.d||''));
  document.getElementById('ticketsBody').innerHTML = tlist.map(r=>
    "<tr><td><a href='"+esc(r.u)+"' target='_blank'>"+esc(r.n)+"</a></td><td>"+esc(r.cid)+"</td><td>"+
    esc(r.p)+"</td><td>"+esc(r.ty)+"</td><td>"+esc(r.t)+"</td><td>"+esc(r.cr)+"</td><td>"+esc(r.st)+
    "</td><td class='num'>"+r.ac+"</td><td>"+esc(r.d)+"</td></tr>").join('')
    || "<tr><td colspan='9'>No tickets found.</td></tr>";
  document.getElementById('tkCount').textContent = recs.length;
}

document.getElementById('filterInput').addEventListener('input', recompute);
document.getElementById('dateFrom').addEventListener('change', recompute);
document.getElementById('dateTo').addEventListener('change', recompute);
document.getElementById('resetBtn').addEventListener('click', function(){
  document.getElementById('filterInput').value = DEFAULT_PREFIXES.join(', ');
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  recompute();
});
recompute();
</script>
</body>
</html>
"""


def normalize_prefixes(prefixes) -> list[str]:
    """Clean a list/iterable of prefixes: trim, drop a trailing '*', drop blanks."""
    out = [str(p).strip().rstrip("*").strip() for p in prefixes]
    return [p for p in out if p]


def generate_report(out_path: str, prefixes=("UD-", "DH-"), log=print, close=False,
                    data_path: str = None) -> dict:
    """Run the full pipeline and write the HTML report. Returns a summary dict.

    Shared entry point for the CLI (main) and the app's CustomerReportService.
    `close=True` force-closes the shared Znuny HTTP session (CLI only; inside the
    app the session is kept alive for the sync worker to reuse).
    `data_path`, if given, also writes a JSON sidecar ({generated_at, prefixes,
    records}) that the app's native (non-iframe) report UI renders from.
    """
    prefixes = normalize_prefixes(prefixes) or ["UD-", "DH-"]
    client = ZnunyClient()
    log(f"Znuny: {client.base_url}")
    log(f"Searching prefixes: {', '.join(p + '*' for p in prefixes)}")

    ticket_ids = search_ticket_ids(client, prefixes, log=log)
    log(f"Total unique tickets to fetch: {len(ticket_ids)}")
    tickets = fetch_tickets(client, ticket_ids, log=log) if ticket_ids else []
    rows, users, customers, stats = analyze(tickets, client, prefixes)
    render_html(rows, users, customers, stats, prefixes, out_path)
    if data_path:
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "prefixes": prefixes,
            "records": stats["records"],
        }
        os.makedirs(os.path.dirname(os.path.abspath(data_path)), exist_ok=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    if close:
        client.close(force=True)

    summary = {
        "prefixes": prefixes,
        "tickets": len(rows),
        "customers": len(customers),
        "staff": len(users),
        "site_visits": len(stats["site_visits"]),
        "out_path": out_path,
    }
    log(f"Report written: {out_path} ({summary['tickets']} tickets, "
        f"{summary['customers']} customers, {summary['staff']} staff, "
        f"{summary['site_visits']} site visits)")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Znuny customer-performance analyzer")
    parser.add_argument("--prefixes", default="UD-,DH-",
                        help="Comma-separated title code prefixes; a trailing '*' is "
                             "optional. e.g. 'UD-,DH-' or 'UD-16-*,UD-15-*,DH-10-*,DH-01-*'")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                        "customer_performance_report.html"),
                        help="Output HTML path")
    args = parser.parse_args()

    prefixes = normalize_prefixes(args.prefixes.split(","))
    if not prefixes:
        print("No prefixes given.")
        return 1

    generate_report(args.out, prefixes, log=print, close=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())