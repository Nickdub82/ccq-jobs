/* ==========================================================
   Map page — Leaflet + OpenStreetMap (free, no API key)
   If you want Google Maps, swap this out later.
   ========================================================== */
'use strict';

const API = window.CCQ_CONFIG.API_BASE;

// Montreal default center
const MONTREAL = [45.5017, -73.5673];

// Initialize map
const map = L.map('map').setView(MONTREAL, 11);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}).addTo(map);

// Custom hazard-yellow pin icon
const pinIcon = L.divIcon({
    className: 'ccq-pin',
    html: `
        <div style="
            width: 28px; height: 28px;
            background: #f4c430;
            border: 2.5px solid #0f1724;
            border-radius: 50% 50% 50% 0;
            transform: rotate(-45deg);
            display:flex;align-items:center;justify-content:center;
            box-shadow: 2px 2px 0 #0f1724;
        ">
            <span style="transform:rotate(45deg);font-family:'JetBrains Mono',monospace;font-weight:700;font-size:10px;color:#0f1724;">●</span>
        </div>
    `,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
    popupAnchor: [0, -28],
});

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function popupHtml(job) {
    const employer = job.employer ? escapeHtml(job.employer.name) : 'Employeur inconnu';
    const loc = escapeHtml(job.location_text || job.city || '');
    const type = escapeHtml(job.job_type || '');
    const salary = escapeHtml(job.salary_text || '');
    const source = escapeHtml(job.source?.display_name || 'Source');
    const ccq = job.is_ccq ? '<span class="job-ccq-badge">✓ CCQ</span>' : '';

    return `
        <div class="map-popup">
            <h4>${escapeHtml(job.title)}</h4>
            ${ccq}
            <div class="employer">${employer}</div>
            <div class="meta">
                ${loc ? `📍 ${loc}<br>` : ''}
                ${type ? `${type}<br>` : ''}
                ${salary ? `💰 ${salary}<br>` : ''}
                Source: <strong>${source}</strong>
            </div>
            <a href="${escapeHtml(job.original_url)}" target="_blank" rel="noopener">
                Voir l'offre →
            </a>
        </div>
    `;
}

async function loadPins() {
    try {
        const res = await fetch(`${API}/api/jobs/map/pins`);
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        const jobs = await res.json();

        document.getElementById('pin-count').textContent = jobs.length;

        if (!jobs.length) return;

        const bounds = [];
        jobs.forEach(job => {
            if (!job.latitude || !job.longitude) return;
            const marker = L.marker([job.latitude, job.longitude], { icon: pinIcon });
            marker.bindPopup(popupHtml(job));
            marker.addTo(map);
            bounds.push([job.latitude, job.longitude]);
        });

        if (bounds.length > 1) {
            map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
        } else if (bounds.length === 1) {
            map.setView(bounds[0], 13);
        }
    } catch (err) {
        console.error('Failed to load pins:', err);
        document.getElementById('pin-count').textContent = 'erreur';
    }
}

loadPins();
