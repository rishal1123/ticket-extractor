// Prevent browser caching of API responses
const _originalFetch = window.fetch;
window.fetch = function(url, options = {}) {
    if (typeof url === 'string' && url.startsWith('/')) {
        options.cache = options.cache || 'no-store';
    }
    return _originalFetch.call(this, url, options);
};

// Maldives timezone (UTC+5)
const MVT_OFFSET = 5 * 60;

function toMaldivesTime(dateStr) {
    if (!dateStr) return null;
    const date = new Date(dateStr);
    const utc = date.getTime() + (date.getTimezoneOffset() * 60000);
    return new Date(utc + (MVT_OFFSET * 60000));
}

function formatMaldivesDateTime(dateStr) {
    if (!dateStr) return '-';
    const mvtDate = toMaldivesTime(dateStr);
    return mvtDate.toLocaleString('en-GB', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    });
}

function formatMaldivesDateTimeFull(dateStr) {
    if (!dateStr) return '-';
    const mvtDate = toMaldivesTime(dateStr);
    return mvtDate.toLocaleString('en-GB', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    }) + ' MVT';
}

function getNowMaldives() {
    const now = new Date();
    const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
    return new Date(utc + (MVT_OFFSET * 60000));
}

function formatTimeDiff(ms) {
    if (!ms || ms < 0) return '-';
    const minutes = Math.floor(ms / 60000);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}d ${hours % 24}h`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    return `${minutes}m`;
}

function formatSiteVisitDuration(minutes) {
    if (minutes === null || minutes === undefined) return '-';
    if (minutes < 60) return `${Math.round(minutes)}m`;
    const hours = Math.floor(minutes / 60);
    const mins = Math.round(minutes % 60);
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
}

function formatRelativeTime(dateStr) {
    if (!dateStr) return '--';
    const date = toMaldivesTime(dateStr);
    const now = getNowMaldives();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return diffMins + 'm ago';
    return diffHours + 'h ago';
}

function getDateRange(filter) {
    const now = getNowMaldives();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    switch (filter) {
        case 'today':
            return { start: today, end: null };
        case 'yesterday':
            const yesterday = new Date(today);
            yesterday.setDate(yesterday.getDate() - 1);
            return { start: yesterday, end: today };
        case 'week':
            const weekAgo = new Date(today);
            weekAgo.setDate(weekAgo.getDate() - 7);
            return { start: weekAgo, end: null };
        default:
            return { start: null, end: null };
    }
}

function showLoading(show) {
    document.getElementById('loadingOverlay').classList.toggle('show', show);
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function sanitizeUrl(url) {
    if (!url) return '';
    try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return url;
    } catch (e) {}
    return '';
}

// Ticket detail modal - shared between dashboard and tickets page
async function showTicketDetail(ticketId, callbacks = {}) {
    showLoading(true);
    window.currentTicketId = ticketId;

    try {
        const [ticketResponse, znunyResponse, siteVisitsResponse] = await Promise.all([
            fetch(`/api/tickets/${ticketId}`),
            fetch(`/api/tickets/${ticketId}/znuny-articles`),
            fetch(`/api/tickets/${ticketId}/site-visits`)
        ]);

        if (!ticketResponse.ok) {
            throw new Error(`Failed to load ticket: ${ticketResponse.status}`);
        }

        const ticketData = await ticketResponse.json();
        const znunyData = znunyResponse.ok ? await znunyResponse.json() : { articles: [] };
        const siteVisitsData = siteVisitsResponse.ok ? await siteVisitsResponse.json() : { visits: [] };

        const ticket = ticketData.ticket || ticketData;

        // Check if ticket data is valid
        if (!ticket || !ticket.portal) {
            console.error('Invalid ticket data:', ticketData);
            alert('Could not load ticket details. The ticket may not exist.');
            showLoading(false);
            return;
        }

        const siteVisits = siteVisitsData.visits || [];
        const znunyArticles = (znunyData.articles || []).sort((a, b) => {
            // Sort by created_at descending (newest first)
            const dateA = a.created_at ? new Date(a.created_at) : new Date(0);
            const dateB = b.created_at ? new Date(b.created_at) : new Date(0);
            return dateB - dateA;
        });

        document.getElementById('modalTicketId').textContent = `${ticket.portal.toUpperCase()} - ${ticket.ticket_id}`;

        // Calculate times
        let timeToCreate = '-';
        let timeToComplete = '-';
        if (ticket.created_at && ticket.znuny_created_at) {
            const diffMs = new Date(ticket.znuny_created_at) - new Date(ticket.created_at);
            timeToCreate = formatTimeDiff(diffMs);
        }
        if (ticket.znuny_created_at && ticket.completed_at) {
            const diffMs = new Date(ticket.completed_at) - new Date(ticket.znuny_created_at);
            timeToComplete = formatTimeDiff(diffMs);
        }

        // Build Znuny section
        let znunySection = '';
        if (ticket.in_znuny) {
            znunySection = `
                <div class="col-12 mt-3">
                    <div class="section-title"><i class="bi bi-box-arrow-in-right"></i> Znuny Details</div>
                    <div class="row g-2 mb-3">
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Znuny Ticket #</div>
                                <div class="value">
                                    ${sanitizeUrl(ticket.znuny_url)
                                        ? `<a href="${sanitizeUrl(ticket.znuny_url)}" target="_blank" class="text-decoration-none">${ticket.znuny_ticket_id} <i class="bi bi-box-arrow-up-right small"></i></a>`
                                        : (ticket.znuny_ticket_id || '-')}
                                </div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Created By</div>
                                <div class="value">${ticket.znuny_created_by || '-'}</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Time to Create</div>
                                <div class="value">${timeToCreate !== '-' ? `<span class="badge bg-info">${timeToCreate}</span>` : '-'}</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Time to Complete</div>
                                <div class="value">${timeToComplete !== '-' ? `<span class="badge bg-success">${timeToComplete}</span>` : '-'}</div>
                            </div>
                        </div>
                    </div>
                    ${(ticket.znuny_state || ticket.znuny_queue || ticket.znuny_owner || ticket.znuny_priority) ? `
                    <div class="row g-2 mb-3">
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">State</div>
                                <div class="value">${ticket.znuny_state ? `<span class="badge bg-info">${escapeHtml(ticket.znuny_state)}</span>` : '-'}</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Queue</div>
                                <div class="value">${ticket.znuny_queue ? `<span class="badge bg-secondary">${escapeHtml(ticket.znuny_queue)}</span>` : '-'}</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Owner</div>
                                <div class="value">${escapeHtml(ticket.znuny_owner) || '-'}</div>
                            </div>
                        </div>
                        <div class="col-6 col-md-3">
                            <div class="info-card">
                                <div class="label">Priority</div>
                                <div class="value">${ticket.znuny_priority ? `<span class="badge bg-warning text-dark">${escapeHtml(ticket.znuny_priority)}</span>` : '-'}</div>
                            </div>
                        </div>
                    </div>
                    ` : ''}
                    ${ticket.znuny_address ? `
                    <div class="alert alert-warning py-2 mb-3">
                        <i class="bi bi-geo-alt me-1"></i>
                        <strong>Znuny Address:</strong> ${escapeHtml(ticket.znuny_address)}
                    </div>
                    ` : ''}
                    ${znunyArticles.length > 0 ? `
                    <div class="section-title"><i class="bi bi-journal-text"></i> Znuny Articles (${znunyArticles.length})</div>
                    <div class="notes-container">
                        <div class="accordion" id="articlesAccordion">
                            ${znunyArticles.map((a, idx) => {
                                const isSiteVisit = (a.subject || '').toLowerCase().includes('site visit') || (a.subject || '').toLowerCase().includes('preventative maintenance');
                                const articleClass = isSiteVisit ? 'article-sitevisit' : a.via === 'Phone' ? 'article-phone' : a.via === 'Internal' ? 'article-internal' : 'article-email';
                                const isFirst = idx === 0;
                                const preview = a.body ? a.body.replace(/\\n/g, ' ').substring(0, 80) : (a.subject || '');
                                const authorLine = a.via === 'Phone' && a.sender && a.created_by && a.sender !== a.created_by
                                    ? `${escapeHtml(a.created_by)} <span class="text-muted small">(caller: ${escapeHtml(a.sender)})</span>`
                                    : escapeHtml(a.created_by || a.sender);
                                return `
                                <div class="accordion-item ${articleClass}">
                                    <h2 class="accordion-header">
                                        <button class="accordion-button ${isFirst ? '' : 'collapsed'} py-2" type="button" data-bs-toggle="collapse" data-bs-target="#article${idx}">
                                            <div class="article-meta">
                                                <span class="badge bg-secondary">#${a.article_number}</span>
                                                <span class="badge bg-${a.via === 'Internal' ? 'info' : a.via === 'Phone' ? 'warning' : isSiteVisit ? 'success' : 'secondary'}">${isSiteVisit ? 'Site Visit' : a.via}</span>
                                                <strong>${authorLine}</strong>
                                                <span class="article-preview">${escapeHtml(preview)}</span>
                                            </div>
                                            <small class="article-time text-muted">${a.created_at_str || '-'}</small>
                                        </button>
                                    </h2>
                                    <div id="article${idx}" class="accordion-collapse collapse ${isFirst ? 'show' : ''}" data-bs-parent="#articlesAccordion">
                                        <div class="accordion-body py-2">
                                            <div class="article-subject"><i class="bi bi-chat-square-text me-1"></i>${escapeHtml(a.subject)}</div>
                                            ${a.body ? `<div class="article-body-content">${escapeHtml(a.body).replace(/\\n/g, '<br>')}</div>` : '<p class="text-muted small mb-0"><em>Body not fetched yet - click Sync Znuny Data to load</em></p>'}
                                        </div>
                                    </div>
                                </div>`;
                            }).join('')}
                        </div>
                    </div>
                    ` : '<p class="text-muted small">No articles synced. Click "Sync Znuny Data" to fetch.</p>'}
                </div>
            `;
        }

        // Build Site Visits section
        let siteVisitsSection = '';
        if (siteVisits.length > 0) {
            siteVisitsSection = `
                <div class="col-12 mt-3">
                    <div class="section-title"><i class="bi bi-geo-alt"></i> Site Visits (${siteVisits.length})</div>
                    <div class="table-responsive">
                        <table class="table table-sm table-hover mb-0">
                            <thead class="table-light">
                                <tr>
                                    <th>Date</th>
                                    <th>Time</th>
                                    <th>Address</th>
                                    <th>Assigned To</th>
                                    <th class="d-none d-sm-table-cell">Type</th>
                                    <th>Status</th>
                                    <th>Duration</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${siteVisits.map(v => `
                                    <tr>
                                        <td>${v.visit_date || '-'}</td>
                                        <td><code>${v.scheduled_time || '-'}</code></td>
                                        <td>${v.address ? `<small>${escapeHtml(v.address)}</small>${v.customer_name ? `<br><small class="text-muted">${escapeHtml(v.customer_name)}</small>` : ''}` : '-'}</td>
                                        <td><strong>${v.assigned_to || '-'}</strong></td>
                                        <td class="d-none d-sm-table-cell">${v.site_type || '-'}</td>
                                        <td>
                                            <span class="badge bg-${v.status === 'completed' ? 'success' : 'warning'}">
                                                ${v.status}
                                            </span>
                                        </td>
                                        <td>
                                            ${v.time_taken_minutes !== null
                                                ? `<span class="badge bg-${v.time_taken_minutes <= 60 ? 'success' : v.time_taken_minutes <= 240 ? 'warning' : 'danger'}">${formatSiteVisitDuration(v.time_taken_minutes)}</span>`
                                                : '-'}
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        const body = document.getElementById('ticketModalBody');
        body.innerHTML = `
            <div class="row g-3">
                <!-- Ticket Info -->
                <div class="col-md-6">
                    <div class="section-title"><i class="bi bi-info-circle"></i> Ticket Information</div>
                    <div class="row g-2">
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Portal</div>
                                <div class="value"><span class="badge badge-portal badge-${ticket.portal}">${ticket.portal}</span></div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Ticket ID</div>
                                <div class="value">
                                    ${sanitizeUrl(ticket.portal_url)
                                        ? `<a href="${sanitizeUrl(ticket.portal_url)}" target="_blank" class="text-decoration-none">${ticket.ticket_id} <i class="bi bi-box-arrow-up-right small"></i></a>`
                                        : ticket.ticket_id}
                                </div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Account</div>
                                <div class="value">${ticket.account || '-'}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Type</div>
                                <div class="value">${ticket.ticket_type || '-'}</div>
                            </div>
                        </div>
                        <div class="col-12">
                            <div class="info-card">
                                <div class="label">Customer</div>
                                <div class="value">${escapeHtml(ticket.customer_name) || '-'}</div>
                            </div>
                        </div>
                        <div class="col-12">
                            <div class="info-card">
                                <div class="label">Address</div>
                                <div class="value">${escapeHtml(ticket.address) || '-'}</div>
                            </div>
                        </div>
                    </div>
                </div>
                <!-- Status & Timestamps -->
                <div class="col-md-6">
                    <div class="section-title"><i class="bi bi-clock-history"></i> Status & Timestamps</div>
                    <div class="row g-2">
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Status</div>
                                <div class="value">${ticket.status || '-'}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">In Znuny</div>
                                <div class="value">
                                    <span class="znuny-status ${ticket.in_znuny ? 'znuny-yes' : 'znuny-no'}">
                                        ${ticket.in_znuny ? '<i class="bi bi-check-circle-fill"></i> Yes' : '<i class="bi bi-x-circle-fill"></i> No'}
                                    </span>
                                </div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Portal Created</div>
                                <div class="value small">${formatMaldivesDateTimeFull(ticket.portal_created_at)}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Last Updated</div>
                                <div class="value small">${formatMaldivesDateTimeFull(ticket.updated_at)}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Entered to Extractor</div>
                                <div class="value small">${formatMaldivesDateTimeFull(ticket.created_at)}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Znuny Created</div>
                                <div class="value small">${formatMaldivesDateTimeFull(ticket.znuny_created_at)}</div>
                            </div>
                        </div>
                        <div class="col-6">
                            <div class="info-card">
                                <div class="label">Completed</div>
                                <div class="value small">${ticket.completed_at ? formatMaldivesDateTimeFull(ticket.completed_at) : '<span class="text-success">Active</span>'}</div>
                            </div>
                        </div>
                    </div>
                </div>
                ${znunySection}
                ${siteVisitsSection}
                <!-- Portal Notes -->
                <div class="col-12 mt-3">
                    <div class="section-title"><i class="bi bi-chat-left-text"></i> Portal Notes</div>
                    <div class="notes-container">
                        ${ticket.notes ? `<pre class="bg-light p-3 rounded small mb-0" style="white-space: pre-wrap;">${escapeHtml(ticket.notes)}</pre>` : '<p class="text-muted small">No notes</p>'}
                    </div>
                </div>
            </div>
        `;

        document.getElementById('checkZnunyBtn').onclick = () => checkZnuny(ticketId, callbacks);
        document.getElementById('syncZnunyBtn').onclick = () => syncZnunyData(ticketId, callbacks);

        // Format Ticket button: only for ISP tickets not yet in Znuny — opens the
        // standardized formatted block (same as the row button) in a new tab.
        const formatBtn = document.getElementById('formatTicketBtn');
        if (formatBtn) {
            if (!ticket.in_znuny) {
                formatBtn.href = `/tickets/${ticketId}/format`;
                formatBtn.style.display = '';
            } else {
                formatBtn.style.display = 'none';
                formatBtn.removeAttribute('href');
            }
        }

        // Use getOrCreateInstance to avoid multiple modal instances causing backdrop issues
        const modalEl = document.getElementById('ticketModal');
        const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();

    } catch (error) {
        console.error('Error loading ticket detail:', error);
    } finally {
        showLoading(false);
    }
}

async function checkZnuny(ticketId, callbacks = {}) {
    showLoading(true);
    try {
        const response = await fetch(`/api/tickets/${ticketId}/check-znuny`, { method: 'POST' });
        const data = await response.json();

        alert(data.in_znuny
            ? `Ticket found in Znuny! (ID: ${data.znuny_ticket_id})`
            : 'Ticket not found in Znuny'
        );

        if (callbacks.onUpdate) callbacks.onUpdate();
        if (window.currentTicketId === ticketId) showTicketDetail(ticketId, callbacks);

    } catch (error) {
        console.error('Error checking Znuny:', error);
        alert('Error checking Znuny status');
    } finally {
        showLoading(false);
    }
}

async function syncZnunyData(ticketId, callbacks = {}) {
    showLoading(true);
    try {
        const response = await fetch(`/api/tickets/${ticketId}/sync-znuny`, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            alert('Znuny data synced successfully!');
            showTicketDetail(ticketId, callbacks);
        } else {
            alert(data.message || 'Failed to sync Znuny data');
        }

    } catch (error) {
        console.error('Error syncing Znuny:', error);
        alert('Error syncing Znuny data');
    } finally {
        showLoading(false);
    }
}

// Render tickets table row
function renderTicketRow(ticket, onClick) {
    const row = document.createElement('tr');
    row.className = 'ticket-row' + (ticket.completed_at ? ' completed' : '');
    row.onclick = onClick;

    const znunyIcon = ticket.in_znuny
        ? '<i class="bi bi-check-circle-fill znuny-yes"></i>'
        : '<i class="bi bi-x-circle-fill znuny-no"></i>';

    // Not-in-Znuny ISP tickets: a "Format" button opens the standardized,
    // formatter-generated ticket block in a new tab (stops the row click).
    const formatBtn = (!ticket.in_znuny && ticket.id)
        ? `<a href="/tickets/${ticket.id}/format" target="_blank" rel="noopener" class="btn btn-sm btn-outline-primary py-0 px-1 ms-1" title="Generate formatted ticket" onclick="event.stopPropagation();"><i class="bi bi-file-earmark-text"></i></a>`
        : '';

    let timeToCreate = '-';
    if (ticket.created_at && ticket.znuny_created_at) {
        const extractorDate = new Date(ticket.created_at);
        const znunyDate = new Date(ticket.znuny_created_at);
        const diffMs = znunyDate - extractorDate;
        timeToCreate = `<span class="badge bg-info time-badge">${formatTimeDiff(diffMs)}</span>`;
    }

    row.innerHTML = `
        <td><span class="badge badge-portal badge-${ticket.portal}">${ticket.portal}</span></td>
        <td><strong>${ticket.ticket_id}</strong></td>
        <td class="text-truncate" style="max-width: 150px;" title="${escapeHtml(ticket.customer_name || '')}">${escapeHtml(ticket.customer_name) || '-'}</td>
        <td class="text-truncate d-none d-md-table-cell" style="max-width: 180px;" title="${escapeHtml(ticket.address || '')}">${escapeHtml(ticket.address) || '-'}</td>
        <td class="d-none d-lg-table-cell">${escapeHtml(ticket.ticket_type) || '-'}</td>
        <td class="d-none d-sm-table-cell">${escapeHtml(ticket.status) || '-'}</td>
        <td class="d-none d-md-table-cell">
            <small>${formatMaldivesDateTime(ticket.created_at)}</small><br>
            <small class="text-muted">${formatRelativeTime(ticket.created_at)}</small>
        </td>
        <td class="znuny-status">${znunyIcon}${formatBtn}</td>
        <td>${timeToCreate}</td>
        <td class="d-none d-sm-table-cell"><small>${escapeHtml(ticket.znuny_created_by) || '-'}</small></td>
    `;
    return row;
}

// Render pagination
function renderPagination(total, currentPage, pageSize, goToPageFn) {
    const totalPages = Math.ceil(total / pageSize);
    const info = document.getElementById('paginationInfo');
    const start = currentPage * pageSize + 1;
    const end = Math.min((currentPage + 1) * pageSize, total);
    info.textContent = total > 0 ? `Showing ${start}-${end} of ${total}` : 'No tickets';

    const pagination = document.getElementById('pagination');
    pagination.innerHTML = '';

    if (totalPages <= 1) return;

    pagination.innerHTML += `
        <li class="page-item ${currentPage === 0 ? 'disabled' : ''}">
            <a class="page-link" href="#" onclick="${goToPageFn}(${currentPage - 1}); return false;">&laquo;</a>
        </li>
    `;

    for (let i = 0; i < totalPages && i < 5; i++) {
        pagination.innerHTML += `
            <li class="page-item ${i === currentPage ? 'active' : ''}">
                <a class="page-link" href="#" onclick="${goToPageFn}(${i}); return false;">${i + 1}</a>
            </li>
        `;
    }

    pagination.innerHTML += `
        <li class="page-item ${currentPage >= totalPages - 1 ? 'disabled' : ''}">
            <a class="page-link" href="#" onclick="${goToPageFn}(${currentPage + 1}); return false;">&raquo;</a>
        </li>
    `;
}
