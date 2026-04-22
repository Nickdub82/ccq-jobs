/* ==========================================================
   CCQ Jobs Portal — main listing logic
   ========================================================== */
'use strict';

const API = window.CCQ_CONFIG.API_BASE;

const state = {
    offset: 0,
    limit: 20,
    filters: {
        region: '',
        trade: '',
        ccq_only: true,
        search: '',
    },
    total: 0,
};

// -- DOM helpers --
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

// -- Date formatting --
function formatDate(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        const now = new Date();
        const diffMs = now - d;
        const days = Math.floor(diffMs / (1000 * 60 * 60 * 24));
        if (days === 0) return "Aujourd'hui";
        if (days === 1) return "Hier";
        if (days < 7) return `Il y a ${days} jours`;
        if (days < 30) return `Il y a ${Math.floor(days / 7)} semaines`;
        return d.toLocaleDateString('fr-CA', { day: 'numeric', month: 'short', year: 'numeric' });
    } catch { return ''; }
}

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

// -- Fetch jobs --
async function fetchJobs() {
    const list = $('#job-list');
    list.innerHTML = '<div class="loading-state">Chargement des offres…</div>';

    const params = new URLSearchParams({
        limit: state.limit,
        offset: state.offset,
        ccq_only: state.filters.ccq_only,
    });
    if (state.filters.region) params.append('region', state.filters.region);
    if (state.filters.trade) params.append('trade', state.filters.trade);
    if (state.filters.search) params.append('search', state.filters.search);

    try {
        const res = await fetch(`${API}/api/jobs?${params}`);
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        const data = await res.json();

        state.total = data.total;
        $('#count').textContent = data.total;

        renderJobs(data.items);
        renderPagination();
    } catch (err) {
        console.error(err);
        list.innerHTML = `
            <div class="empty-state">
                <h3>Erreur de chargement</h3>
                <p>Impossible de joindre le serveur. Réessayez dans un instant.</p>
                <p style="margin-top:8px;font-size:11px;">${escapeHtml(err.message)}</p>
            </div>
        `;
    }
}

// -- Render --
function renderJobs(jobs) {
    const list = $('#job-list');
    list.innerHTML = '';

    if (!jobs.length) {
        list.innerHTML = `
            <div class="empty-state">
                <h3>Aucune offre trouvée</h3>
                <p>Ajustez vos filtres ou revenez plus tard — le système se met à jour toutes les 2 heures.</p>
            </div>
        `;
        return;
    }

    jobs.forEach(job => list.appendChild(jobCard(job)));
}

function jobCard(job) {
    const card = el('article', { class: 'job-card' });

    // Top row: title + CCQ badge
    const top = el('div', { class: 'job-card-top' });
    top.appendChild(el('h3', { class: 'job-title' }, job.title));
    if (job.is_ccq) {
        top.appendChild(el('span', { class: 'job-ccq-badge' }, '✓ CCQ'));
    }
    card.appendChild(top);

    // Employer
    if (job.employer) {
        card.appendChild(el('div', { class: 'job-employer' }, job.employer.name));
    }

    // Meta row
    const meta = el('div', { class: 'job-meta' });
    if (job.location_text || job.city) {
        meta.appendChild(el('span', {}, `📍 ${job.location_text || job.city}`));
    }
    if (job.job_type) meta.appendChild(el('span', {}, job.job_type));
    if (job.salary_text) meta.appendChild(el('span', {}, `💰 ${job.salary_text}`));
    if (job.posted_at) meta.appendChild(el('span', {}, formatDate(job.posted_at)));
    else if (job.first_seen_at) meta.appendChild(el('span', {}, formatDate(job.first_seen_at)));
    card.appendChild(meta);

    // Description
    if (job.description) {
        card.appendChild(el('p', { class: 'job-description' }, job.description));
    }

    // Footer
    const footer = el('div', { class: 'job-footer' });

    const sourceEl = el('div', { class: 'job-source' });
    const sourceLabel = job.source?.display_name || 'Source';
    sourceEl.appendChild(el('span', { html: `Source : <strong>${escapeHtml(sourceLabel)}</strong>` }));
    footer.appendChild(sourceEl);

    const applyLink = el('a', {
        class: 'job-apply',
        href: job.original_url,
        target: '_blank',
        rel: 'noopener noreferrer',
    }, 'Voir l\'offre originale →');
    footer.appendChild(applyLink);

    card.appendChild(footer);

    // Make whole card clickable (except the apply link itself)
    card.addEventListener('click', (e) => {
        if (e.target.closest('a')) return;
        window.open(job.original_url, '_blank', 'noopener');
    });

    return card;
}

function renderPagination() {
    const pag = $('#pagination');
    if (state.total <= state.limit) {
        pag.style.display = 'none';
        return;
    }
    pag.style.display = 'flex';
    $('#prev-page').disabled = state.offset === 0;
    $('#next-page').disabled = state.offset + state.limit >= state.total;
}

// -- Event listeners --
$('#btn-apply').addEventListener('click', () => {
    state.filters.region   = $('#f-region').value;
    state.filters.trade    = $('#f-trade').value;
    state.filters.ccq_only = $('#f-ccq-only').checked;
    state.filters.search   = $('#f-search').value.trim();
    state.offset = 0;
    fetchJobs();
});

$('#btn-reset').addEventListener('click', () => {
    $('#f-region').value = '';
    $('#f-trade').value = '';
    $('#f-ccq-only').checked = true;
    $('#f-search').value = '';
    state.filters = { region: '', trade: '', ccq_only: true, search: '' };
    state.offset = 0;
    fetchJobs();
});

$('#f-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') $('#btn-apply').click();
});

$('#prev-page').addEventListener('click', () => {
    state.offset = Math.max(0, state.offset - state.limit);
    fetchJobs();
    window.scrollTo({ top: 0, behavior: 'smooth' });
});

$('#next-page').addEventListener('click', () => {
    state.offset += state.limit;
    fetchJobs();
    window.scrollTo({ top: 0, behavior: 'smooth' });
});

// -- Initial load --
fetchJobs();
