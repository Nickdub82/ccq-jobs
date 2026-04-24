/* ==========================================================
   Local 349 — Portail emploi
   Fetches jobs from backend, renders cards, handles filters,
   populates hero stats, and draws the map.
   ========================================================== */
'use strict';

const API = window.CCQ_CONFIG.API_BASE;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

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

function formatRelative(iso) {
    if (!iso) return '—';
    const then = new Date(iso);
    const diffMs = Date.now() - then.getTime();
    const hours = Math.floor(diffMs / (1000 * 60 * 60));
    if (hours < 1) return 'À l\'instant';
    if (hours < 24) return `Il y a ${hours}h`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `Il y a ${days}j`;
    return then.toLocaleDateString('fr-CA', { day: 'numeric', month: 'short' });
}

// ========================================
// DATA STATE
// ========================================
let allJobs = [];
let mapInstance = null;
let mapMarkers = [];

// ========================================
// FETCH JOBS
// ========================================
async function fetchJobs() {
    const params = new URLSearchParams();
    const ccqOnly = $('#f-ccq').checked;
    const region = $('#f-region').value;
    const trade = $('#f-trade').value;
    const search = $('#f-search').value.trim();

    if (ccqOnly) params.append('ccq_only', 'true');
    if (region) params.append('region', region);
    if (trade) params.append('trade', trade);
    if (search) params.append('search', search);

    try {
        const res = await fetch(`${API}/api/jobs?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        return data.items || [];
    } catch (err) {
        console.error('Fetch jobs failed:', err);
        return [];
    }
}

// ========================================
// RENDER JOBS
// ========================================
function renderJobs(jobs) {
    const list = $('#jobs-list');
    const countEl = $('#jobs-count');
    list.innerHTML = '';
    countEl.textContent = jobs.length;

    if (!jobs.length) {
        list.appendChild(el('div', { class: 'empty-state' },
            el('h3', {}, 'Aucune offre trouvée'),
            el('p', {}, 'Essayez d\'élargir vos filtres ou réinitialisez la recherche.')
        ));
        return;
    }

    jobs.forEach(job => list.appendChild(jobCard(job)));
}

function jobCard(job) {
    const card = el('div', { class: 'job-card' });

    // Top row: title + CCQ badge
    const top = el('div', { class: 'job-card-top' });
    top.appendChild(el('h3', { class: 'job-title' }, job.title));
    if (job.is_ccq) {
        top.appendChild(el('span', { class: 'job-ccq-badge' }, '✓ CCQ'));
    }
    card.appendChild(top);

    // Employer
    if (job.employer?.name) {
        card.appendChild(el('div', { class: 'job-employer' }, job.employer.name));
    }

    // Meta: location, salary, posted
    const meta = el('div', { class: 'job-meta' });
    if (job.location_text || job.city) {
        meta.appendChild(el('div', { class: 'job-meta-item' }, job.location_text || job.city));
    }
    if (job.salary_text) {
        meta.appendChild(el('div', { class: 'job-meta-item' }, job.salary_text));
    }
    meta.appendChild(el('div', { class: 'job-meta-item' }, formatRelative(job.first_seen_at)));
    card.appendChild(meta);

    // Description
    if (job.description) {
        const short = job.description.length > 260
            ? job.description.slice(0, 260) + '…'
            : job.description;
        card.appendChild(el('p', { class: 'job-description' }, short));
    }

    // Footer: source + CTA
    const footer = el('div', { class: 'job-footer' });
    footer.appendChild(el('div', { class: 'job-source' },
        'Source : ',
        el('strong', {}, job.source?.display_name || 'Web')
    ));

    const cta = el('a', {
        href: job.original_url,
        target: '_blank',
        rel: 'noopener',
        class: 'job-cta',
    },
        el('span', {}, 'Voir l\'offre'),
        el('span', { html: '&rarr;' })
    );
    footer.appendChild(cta);
    card.appendChild(footer);

    return card;
}

// ========================================
// HERO STATS
// ========================================
async function loadHeroStats() {
    try {
        const jobs = await fetchJobs();
        allJobs = jobs;

        $('#stat-total').textContent = jobs.length;
        $('#stat-ccq').textContent = jobs.filter(j => j.is_ccq).length;

        // Most recent update
        const mostRecent = jobs.reduce((max, j) => {
            const t = new Date(j.last_seen_at || j.first_seen_at).getTime();
            return t > max ? t : max;
        }, 0);
        $('#stat-updated').textContent = mostRecent
            ? formatRelative(new Date(mostRecent).toISOString())
            : '—';
    } catch (err) {
        console.error('Stats failed:', err);
    }
}

// ========================================
// FILTERS — populate regions from jobs
// ========================================
function populateRegionFilter(jobs) {
    const regions = [...new Set(jobs
        .map(j => j.region || j.city)
        .filter(Boolean)
    )].sort();

    const select = $('#f-region');
    const current = select.value;

    // Remove old dynamic options (keep only "Toutes")
    [...select.options].slice(1).forEach(o => o.remove());

    regions.forEach(r => {
        select.appendChild(el('option', { value: r }, r));
    });

    if (current) select.value = current;
}

// ========================================
// MAP
// ========================================
function initMap() {
    if (mapInstance) return;
    const mapEl = $('#map');
    if (!mapEl) return;

    mapInstance = L.map('map', {
        center: [46.8, -71.5],  // Quebec province center
        zoom: 6,
        scrollWheelZoom: false,
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap',
        maxZoom: 18,
    }).addTo(mapInstance);
}

async function renderMap(jobs) {
    if (!mapInstance) initMap();

    // Clear old markers
    mapMarkers.forEach(m => m.remove());
    mapMarkers = [];

    // Approximate coords per region (fallback when no precise address)
    const regionCoords = {
        'Montréal': [45.5019, -73.5674],
        'Laval': [45.6066, -73.7124],
        'Longueuil': [45.5312, -73.5180],
        'Québec': [46.8139, -71.2080],
        'Outaouais': [45.4215, -75.6972],
        'Trois-Rivières': [46.3432, -72.5432],
        'Sherbrooke': [45.4042, -71.8929],
        'Saguenay': [48.4280, -71.0679],
        'Gatineau': [45.4765, -75.7013],
        'Repentigny': [45.7423, -73.4505],
        'Cowansville': [45.2077, -72.7468],
    };

    const bounds = [];

    jobs.forEach(job => {
        if (!job.is_approved) return;
        let coords = null;

        // Try precise coords first
        if (job.latitude && job.longitude) {
            coords = [job.latitude, job.longitude];
        } else {
            // Fallback: match city or region
            const key = job.city || job.region || (job.location_text || '').split(',')[0].trim();
            if (regionCoords[key]) {
                coords = regionCoords[key];
            }
        }

        if (!coords) return;

        const marker = L.circleMarker(coords, {
            radius: 10,
            fillColor: '#F4BE1F',
            color: '#0d0d0d',
            weight: 2,
            opacity: 1,
            fillOpacity: 0.85,
        }).addTo(mapInstance);

        const popupHtml = `
            <strong>${escapeHtml(job.title)}</strong><br>
            ${escapeHtml(job.employer?.name || '')}<br>
            ${escapeHtml(job.location_text || job.city || '')}<br>
            <a href="${escapeHtml(job.original_url)}" target="_blank" rel="noopener"
               style="color:#F4BE1F;font-weight:700;margin-top:8px;display:inline-block;">
               Voir l'offre →
            </a>
        `;
        marker.bindPopup(popupHtml);
        mapMarkers.push(marker);
        bounds.push(coords);
    });

    if (bounds.length) {
        mapInstance.fitBounds(bounds, { padding: [40, 40], maxZoom: 10 });
    }
}

// ========================================
// FULL RELOAD
// ========================================
async function reloadAll() {
    const jobs = await fetchJobs();
    allJobs = jobs;
    populateRegionFilter(jobs);
    renderJobs(jobs);

    // Update hero stats
    $('#stat-total').textContent = jobs.length;
    $('#stat-ccq').textContent = jobs.filter(j => j.is_ccq).length;
    const mostRecent = jobs.reduce((max, j) => {
        const t = new Date(j.last_seen_at || j.first_seen_at).getTime();
        return t > max ? t : max;
    }, 0);
    $('#stat-updated').textContent = mostRecent
        ? formatRelative(new Date(mostRecent).toISOString())
        : '—';

    // Render map (only jobs that are approved will be shown)
    renderMap(jobs);
}

// ========================================
// EVENT HANDLERS
// ========================================
$('#btn-apply')?.addEventListener('click', (e) => {
    e.preventDefault();
    reloadAll();
});

$('#btn-reset')?.addEventListener('click', (e) => {
    e.preventDefault();
    $('#f-search').value = '';
    $('#f-region').value = '';
    $('#f-trade').value = '';
    $('#f-ccq').checked = true;
    reloadAll();
});

$('#f-search')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        reloadAll();
    }
});

// ========================================
// INIT
// ========================================
(async () => {
    initMap();
    await reloadAll();
})();
