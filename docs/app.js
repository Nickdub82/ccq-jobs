/* ==========================================================
   Local 349 — Portail emploi
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
// CITY COORDS — all keys lowercase, no accents
// ========================================
const CITIES = {
    'montreal': [45.5019, -73.5674],
    'laval': [45.6066, -73.7124],
    'longueuil': [45.5312, -73.5180],
    'brossard': [45.4582, -73.4663],
    'boucherville': [45.5936, -73.4366],
    'repentigny': [45.7423, -73.4505],
    'terrebonne': [45.6985, -73.6510],
    'mascouche': [45.7497, -73.5988],
    'blainville': [45.6707, -73.8782],
    'mirabel': [45.6493, -74.0814],
    'vaudreuil-dorion': [45.4006, -74.0330],
    'chambly': [45.4461, -73.2893],
    'saint-jean-sur-richelieu': [45.3074, -73.2621],
    'saint-hubert': [45.4958, -73.4166],
    'saint-bruno-de-montarville': [45.5372, -73.3500],
    'anjou': [45.6085, -73.5540],
    'saint-leonard': [45.5934, -73.5987],
    'saint-laurent': [45.5023, -73.7215],
    'verdun': [45.4590, -73.5693],
    'lasalle': [45.4307, -73.6388],
    'pointe-claire': [45.4486, -73.8168],
    'dorval': [45.4500, -73.7477],
    'pierrefonds': [45.4936, -73.8596],
    'quebec': [46.8139, -71.2080],
    'levis': [46.7892, -71.1784],
    'sainte-foy': [46.7688, -71.2894],
    'charlesbourg': [46.8552, -71.2709],
    'beauport': [46.8727, -71.1885],
    'sherbrooke': [45.4042, -71.8929],
    'trois-rivieres': [46.3432, -72.5432],
    'drummondville': [45.8833, -72.4833],
    'granby': [45.4001, -72.7329],
    'saguenay': [48.4280, -71.0679],
    'chicoutimi': [48.4197, -71.0672],
    'jonquiere': [48.4172, -71.2480],
    'gatineau': [45.4765, -75.7013],
    'outaouais': [45.4215, -75.6972],
    'hull': [45.4310, -75.7280],
    'aylmer': [45.3992, -75.8323],
    'rimouski': [48.4489, -68.5230],
    'rouyn-noranda': [48.2363, -79.0240],
    'saint-jerome': [45.7804, -74.0037],
    'cowansville': [45.2077, -72.7468],
    'saint-hyacinthe': [45.6301, -72.9564],
    'thetford mines': [46.1000, -71.3050],
    'victoriaville': [46.0553, -71.9599],
    'sorel-tracy': [46.0432, -73.1138],
    'joliette': [46.0225, -73.4390],
    'shawinigan': [46.5553, -72.7453],
    'magog': [45.2682, -72.1540],
    'alma': [48.5500, -71.6525],
    'matane': [48.8333, -67.5167],
    'baie-comeau': [49.2167, -68.1667],
    'sept-iles': [50.2000, -66.3833],
    'gaspe': [48.8314, -64.4811],
};

function cleanKey(s) {
    if (!s) return '';
    return String(s)
        .toLowerCase()
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/,\s*qc.*$/i, '')
        .trim();
}

function lookupCity(s) {
    const key = cleanKey(s);
    if (!key) return null;
    if (CITIES[key]) return CITIES[key];
    for (const [k, v] of Object.entries(CITIES)) {
        if (key.includes(k) || k.includes(key)) return v;
    }
    return null;
}

// ========================================
// STATE
// ========================================
let allJobs = [];
let mapInstance = null;
let mapMarkers = [];

// ========================================
// FETCH
// ========================================
async function fetchJobs() {
    const params = new URLSearchParams();
    const ccqOnly = $('#f-ccq')?.checked;
    const region = $('#f-region')?.value;
    const trade = $('#f-trade')?.value;
    const search = $('#f-search')?.value.trim();

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
    if (!list) return;
    list.innerHTML = '';
    if (countEl) countEl.textContent = jobs.length;

    if (!jobs.length) {
        list.appendChild(el('div', { class: 'empty-state' },
            el('h3', {}, 'Aucune offre trouvée'),
            el('p', {}, 'Essayez d\'élargir vos filtres.')
        ));
        return;
    }

    jobs.forEach(job => list.appendChild(jobCard(job)));
}

function jobCard(job) {
    const card = el('div', { class: 'job-card' });

    const top = el('div', { class: 'job-card-top' });
    top.appendChild(el('h3', { class: 'job-title' }, job.title));
    if (job.is_ccq) {
        top.appendChild(el('span', { class: 'job-ccq-badge' }, '✓ CCQ'));
    }
    card.appendChild(top);

    if (job.employer?.name) {
        card.appendChild(el('div', { class: 'job-employer' }, job.employer.name));
    }

    const meta = el('div', { class: 'job-meta' });
    if (job.location_text || job.city) {
        meta.appendChild(el('div', { class: 'job-meta-item' }, job.location_text || job.city));
    }
    if (job.salary_text) {
        meta.appendChild(el('div', { class: 'job-meta-item' }, job.salary_text));
    }
    meta.appendChild(el('div', { class: 'job-meta-item' }, formatRelative(job.first_seen_at)));
    card.appendChild(meta);

    if (job.description) {
        const short = job.description.length > 260
            ? job.description.slice(0, 260) + '…'
            : job.description;
        card.appendChild(el('p', { class: 'job-description' }, short));
    }

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
// FILTERS
// ========================================
function populateRegionFilter(jobs) {
    const select = $('#f-region');
    if (!select) return;

    const regions = [...new Set(jobs
        .map(j => (j.location_text || '').split(',')[0].trim() || j.city || j.region)
        .filter(Boolean)
    )].sort();

    const current = select.value;
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
        center: [46.8, -71.5],
        zoom: 6,
        scrollWheelZoom: false,
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap',
        maxZoom: 18,
    }).addTo(mapInstance);
}

function renderMap(jobs) {
    if (!mapInstance) initMap();
    if (!mapInstance) return;

    mapMarkers.forEach(m => m.remove());
    mapMarkers = [];

    const bounds = [];
    let placed = 0;
    let missed = 0;

    jobs.forEach(job => {
        if (!job.is_approved) return;

        let coords = null;

        if (job.latitude && job.longitude) {
            coords = [job.latitude, job.longitude];
        } else {
            coords = lookupCity(job.location_text)
                || lookupCity(job.city)
                || lookupCity(job.region);
        }

        if (!coords) {
            console.warn('Map: no coords for', job.title, '|', job.location_text);
            missed++;
            return;
        }

        placed++;

        const marker = L.circleMarker(coords, {
            radius: 11,
            fillColor: '#F4BE1F',
            color: '#0d0d0d',
            weight: 2,
            opacity: 1,
            fillOpacity: 0.9,
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

    console.info(`Map: ${placed} placed, ${missed} missed`);

    if (bounds.length) {
        mapInstance.fitBounds(bounds, { padding: [60, 60], maxZoom: 10 });
    }
}

// ========================================
// RELOAD
// ========================================
async function reloadAll() {
    const jobs = await fetchJobs();
    allJobs = jobs;

    populateRegionFilter(jobs);
    renderJobs(jobs);

    if ($('#stat-total')) $('#stat-total').textContent = jobs.length;
    if ($('#stat-ccq')) $('#stat-ccq').textContent = jobs.filter(j => j.is_ccq).length;
    const mostRecent = jobs.reduce((max, j) => {
        const t = new Date(j.last_seen_at || j.first_seen_at).getTime();
        return t > max ? t : max;
    }, 0);
    if ($('#stat-updated')) {
        $('#stat-updated').textContent = mostRecent
            ? formatRelative(new Date(mostRecent).toISOString())
            : '—';
    }

    renderMap(jobs);
}

// ========================================
// EVENTS
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
// INIT — wait for DOM to be ready
// ========================================
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        initMap();
        reloadAll();
    });
} else {
    initMap();
    reloadAll();
}
