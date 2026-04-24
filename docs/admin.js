/* ==========================================================
   Admin page logic — login, stats, review queue, approved jobs, runs log
   Password is stored in sessionStorage (cleared on tab close).
   ========================================================== */
'use strict';

const API = window.CCQ_CONFIG.API_BASE;

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
    const e = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => {
        if (k === 'class') e.className = v;
        else if (k === 'html') e.innerHTML = v;
        else if (k.startsWith('on') && typeof v === 'function') {
            e.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (v !== null && v !== undefined) {
            e.setAttribute(k, v);
        }
    });
    children.flat().forEach(c => {
        if (c === null || c === undefined) return;
        e.append(c.nodeType ? c : document.createTextNode(String(c)));
    });
    return e;
};

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function formatDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('fr-CA', {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit'
        });
    } catch { return iso; }
}

// ---------- Auth ----------
function getPassword() {
    return sessionStorage.getItem('ccq_admin_pw') || '';
}

function adminHeaders() {
    return { 'x-admin-password': getPassword(), 'Content-Type': 'application/json' };
}

async function testAuth(pw) {
    const res = await fetch(`${API}/api/admin/stats`, {
        headers: { 'x-admin-password': pw },
    });
    return res.ok;
}

$('#btn-login').addEventListener('click', async () => {
    const pw = $('#admin-password').value.trim();
    $('#login-error').textContent = '';
    if (!pw) {
        $('#login-error').textContent = 'Mot de passe requis.';
        return;
    }
    try {
        const ok = await testAuth(pw);
        if (!ok) {
            $('#login-error').textContent = 'Mot de passe invalide.';
            return;
        }
        sessionStorage.setItem('ccq_admin_pw', pw);
        showDashboard();
    } catch (err) {
        $('#login-error').textContent = `Erreur: ${err.message}`;
    }
});

$('#admin-password')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('#btn-login').click();
});

$('#btn-logout').addEventListener('click', () => {
    sessionStorage.removeItem('ccq_admin_pw');
    location.reload();
});

// ---------- Dashboard ----------
async function showDashboard() {
    $('#login-view').style.display = 'none';
    $('#admin-view').style.display = 'grid';
    await Promise.all([loadStats(), loadReviewQueue(), loadApprovedJobs(), loadRuns()]);
}

async function loadStats() {
    const box = $('#stats-container');
    try {
        const res = await fetch(`${API}/api/admin/stats`, { headers: adminHeaders() });
        if (!res.ok) throw new Error(res.status);
        const s = await res.json();
        box.innerHTML = '';
        box.append(
            statBox('Total offres', s.total_jobs),
            statBox('Approuvées', s.approved),
            statBox('En révision', s.in_review, s.in_review > 0),
            statBox('CCQ confirmées', s.ccq_confirmed),
        );
    } catch (err) {
        box.innerHTML = `<div class="empty-state">Erreur: ${escapeHtml(err.message)}</div>`;
    }
}

function statBox(label, value, highlight = false) {
    return el('div', { class: `stat-box ${highlight ? 'highlight' : ''}` },
        el('div', { class: 'stat-label' }, label),
        el('div', { class: 'stat-value' }, value ?? '—'),
    );
}

// ---------- Review queue ----------
async function loadReviewQueue() {
    const list = $('#review-list');
    try {
        const res = await fetch(`${API}/api/admin/review-queue`, { headers: adminHeaders() });
        if (!res.ok) throw new Error(res.status);
        const data = await res.json();
        list.innerHTML = '';

        if (!data.items.length) {
            list.innerHTML = `
                <div class="empty-state">
                    <h3>✓ File vide</h3>
                    <p>Aucune offre n'attend de révision. Claude est confiant dans tout ce qui est en base.</p>
                </div>
            `;
            return;
        }

        data.items.forEach(job => list.appendChild(reviewCard(job)));
    } catch (err) {
        list.innerHTML = `<div class="empty-state">Erreur: ${escapeHtml(err.message)}</div>`;
    }
}

function reviewCard(job) {
    const card = el('div', { class: 'admin-card review' });

    const top = el('div', { style: 'display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:12px;' });
    top.appendChild(el('div', {},
        el('h3', { style: 'font-family:var(--font-display);font-size:20px;color:var(--navy);margin-bottom:4px;' }, job.title),
        el('div', { style: 'font-family:var(--font-mono);font-size:12px;color:var(--muted);' },
            job.employer?.name || 'Employeur inconnu',
            ' · ',
            job.location_text || job.city || '—'
        ),
    ));
    if (job.is_ccq) top.appendChild(el('span', { class: 'job-ccq-badge' }, '✓ CCQ présumé'));
    card.appendChild(top);

    if (job.ai_confidence != null) {
        const confPct = Math.round(job.ai_confidence * 100);
        card.appendChild(el('div', { style: 'font-family:var(--font-mono);font-size:11px;color:var(--muted);margin-top:8px;' },
            `Confiance Claude: ${confPct}%`
        ));
        const bar = el('div', { class: 'confidence-bar' });
        bar.appendChild(el('span', { style: `width:${confPct}%` }));
        card.appendChild(bar);
    }

    if (job.description) {
        card.appendChild(el('p', {
            style: 'font-size:14px;color:#333;margin-top:12px;line-height:1.5;'
        }, job.description.slice(0, 400) + (job.description.length > 400 ? '…' : '')));
    }

    if (job.ai_notes) {
        card.appendChild(el('div', { class: 'ai-notes', html: `<strong>Note Claude :</strong> ${escapeHtml(job.ai_notes)}` }));
    }

    card.appendChild(el('div', { style: 'margin-top:12px;font-family:var(--font-mono);font-size:11px;' },
        'Source : ',
        el('a', { href: job.original_url, target: '_blank', rel: 'noopener' }, job.original_url)
    ));

    const actions = el('div', { class: 'admin-actions' });
    actions.appendChild(el('button', {
        class: 'btn',
        onclick: () => reviewDecision(job.id, true, card)
    }, '✓ Approuver'));
    actions.appendChild(el('button', {
        class: 'btn btn-danger',
        onclick: () => reviewDecision(job.id, false, card)
    }, '✕ Rejeter'));
    card.appendChild(actions);

    return card;
}

async function reviewDecision(jobId, approve, cardEl) {
    const action = approve ? 'approuver' : 'rejeter';
    if (!confirm(`Confirmez-vous vouloir ${action} cette offre ?`)) return;

    try {
        const res = await fetch(`${API}/api/admin/review/${jobId}`, {
            method: 'POST',
            headers: adminHeaders(),
            body: JSON.stringify({ approve }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        cardEl.style.opacity = '0.4';
        cardEl.style.pointerEvents = 'none';
        setTimeout(() => { cardEl.remove(); loadStats(); loadApprovedJobs(); }, 300);
    } catch (err) {
        alert(`Erreur: ${err.message}`);
    }
}

// ---------- Approved jobs (visible on portal) ----------
async function loadApprovedJobs() {
    const list = $('#approved-list');
    if (!list) return;  // section might not exist in old HTML

    try {
        const res = await fetch(`${API}/api/admin/approved`, { headers: adminHeaders() });
        if (!res.ok) throw new Error(res.status);
        const data = await res.json();
        list.innerHTML = '';

        if (!data.items.length) {
            list.innerHTML = `
                <div class="empty-state">
                    <p>Aucune offre approuvée actuellement.</p>
                </div>
            `;
            return;
        }

        data.items.forEach(job => list.appendChild(approvedCard(job)));
    } catch (err) {
        list.innerHTML = `<div class="empty-state">Erreur: ${escapeHtml(err.message)}</div>`;
    }
}

function approvedCard(job) {
    const card = el('div', { class: 'admin-card approved' });

    const top = el('div', { style: 'display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:8px;' });
    top.appendChild(el('div', {},
        el('h3', { style: 'font-family:var(--font-display);font-size:18px;color:var(--navy);margin-bottom:4px;' }, job.title),
        el('div', { style: 'font-family:var(--font-mono);font-size:12px;color:var(--muted);' },
            job.employer?.name || 'Employeur inconnu',
            ' · ',
            job.location_text || job.city || '—'
        ),
    ));
    if (job.is_ccq) top.appendChild(el('span', { class: 'job-ccq-badge' }, '✓ CCQ'));
    card.appendChild(top);

    if (job.description) {
        card.appendChild(el('p', {
            style: 'font-size:13px;color:#555;margin-top:8px;line-height:1.4;'
        }, job.description.slice(0, 200) + (job.description.length > 200 ? '…' : '')));
    }

    card.appendChild(el('div', { style: 'margin-top:10px;font-family:var(--font-mono);font-size:11px;color:var(--muted);' },
        `ID #${job.id} · ajouté ${formatDate(job.first_seen_at)}`
    ));

    // Delete button
    const actions = el('div', { class: 'admin-actions', style: 'margin-top:10px;' });
    actions.appendChild(el('a', {
        class: 'btn',
        href: job.original_url,
        target: '_blank',
        rel: 'noopener',
        style: 'text-decoration:none;'
    }, 'Voir l\'offre'));
    actions.appendChild(el('button', {
        class: 'btn btn-danger',
        onclick: () => deleteApprovedJob(job.id, job.title, card)
    }, '🗑 Retirer'));
    card.appendChild(actions);

    return card;
}

async function deleteApprovedJob(jobId, title, cardEl) {
    if (!confirm(`Retirer cette offre du portail ?\n\n"${title}"\n\nElle sera supprimée définitivement.`)) return;

    try {
        const res = await fetch(`${API}/api/admin/jobs/${jobId}`, {
            method: 'DELETE',
            headers: adminHeaders(),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        cardEl.style.opacity = '0.4';
        cardEl.style.pointerEvents = 'none';
        setTimeout(() => { cardEl.remove(); loadStats(); }, 300);
    } catch (err) {
        alert(`Erreur: ${err.message}`);
    }
}

// ---------- Scraping runs ----------
async function loadRuns() {
    const list = $('#runs-list');
    try {
        const res = await fetch(`${API}/api/admin/runs?limit=20`, { headers: adminHeaders() });
        if (!res.ok) throw new Error(res.status);
        const runs = await res.json();
        list.innerHTML = '';

        if (!runs.length) {
            list.innerHTML = `<div class="empty-state"><p>Aucun run enregistré encore.</p></div>`;
            return;
        }

        runs.forEach(run => list.appendChild(runCard(run)));
    } catch (err) {
        list.innerHTML = `<div class="empty-state">Erreur: ${escapeHtml(err.message)}</div>`;
    }
}

function runCard(run) {
    const card = el('div', { class: 'admin-card' });
    const statusColor = run.status === 'success' ? 'var(--success)'
                      : run.status === 'failed'  ? 'var(--rust)'
                      : 'var(--muted)';

    card.appendChild(el('div', { style: 'display:flex;justify-content:space-between;align-items:baseline;' },
        el('div', { style: 'font-family:var(--font-mono);font-size:13px;font-weight:700;' },
            `Run #${run.id}`,
        ),
        el('span', {
            style: `font-family:var(--font-mono);font-size:11px;text-transform:uppercase;color:${statusColor};font-weight:700;`,
        }, run.status),
    ));

    card.appendChild(el('div', {
        style: 'font-family:var(--font-mono);font-size:11px;color:var(--muted);margin-top:4px;'
    }, `${formatDate(run.started_at)} → ${run.finished_at ? formatDate(run.finished_at) : 'en cours'}`));

    card.appendChild(el('div', {
        style: 'display:flex;flex-wrap:wrap;gap:20px;margin-top:10px;font-family:var(--font-mono);font-size:12px;'
    },
        el('span', {}, `Scrapés: ${run.jobs_scraped}`),
        el('span', {}, `Nouveaux: ${run.jobs_new}`),
        el('span', {}, `Maj: ${run.jobs_updated}`),
        el('span', {}, `Supprimés: ${run.jobs_removed}`),
        el('span', {}, `Signalés: ${run.jobs_flagged}`),
        el('span', {}, `Appels IA: ${run.ai_calls}`),
    ));

    if (run.error_message) {
        card.appendChild(el('div', {
            class: 'ai-notes',
            style: 'border-left-color: var(--rust); color: var(--rust);',
            html: `<strong>Erreur :</strong> ${escapeHtml(run.error_message)}`
        }));
    }

    return card;
}

// ---------- Init ----------
(async () => {
    const pw = getPassword();
    if (pw) {
        const ok = await testAuth(pw);
        if (ok) { showDashboard(); return; }
        sessionStorage.removeItem('ccq_admin_pw');
    }
})();
