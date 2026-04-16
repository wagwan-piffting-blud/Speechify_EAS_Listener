/* VIN Explorer - chunk sidebar + unit table + hash widget */

let vinChunks = [];
let unitPage = 0;
const UNIT_PER_PAGE = 100;

async function loadVinExplorer() {
    window._vinLoaded = true;
    try {
        vinChunks = await apiJson('/api/vin/chunks');
        renderChunkList();

        // Populate phone filter dropdown
        const sel = document.getElementById('unit-phone-filter');
        const phones = ["aa","ae","ah","ao","aw","ax","ay","b","ch","dx","d","dh",
                        "eh","el","er","en","ey","f","g","hh","ih","ix","iy","jh",
                        "k","l","m","n","ng","ow","oy","p","pau","r","s","sh","t",
                        "th","uh","uw","v","w","xx","y","z","zh"];
        phones.forEach(ph => {
            const opt = document.createElement('option');
            opt.value = ph;
            opt.textContent = ph;
            sel.appendChild(opt);
        });
    } catch (e) {
        console.error('Failed to load VIN:', e);
    }
}

function renderChunkList() {
    const ul = document.getElementById('chunk-list');
    ul.innerHTML = '';
    vinChunks.forEach(c => {
        const li = document.createElement('li');
        li.title = CHUNK_DESCRIPTIONS[c.tag] || '';
        li.innerHTML = `<span>${c.tag}</span><span class="size">${fmtBytes(c.size)}</span>`;
        li.addEventListener('click', () => selectChunk(c, li));
        ul.appendChild(li);
    });
}

function selectChunk(chunk, li) {
    document.querySelectorAll('#chunk-list li').forEach(l => l.classList.remove('active'));
    li.classList.add('active');

    // Hide all detail sections
    document.getElementById('unit-table-container').style.display = 'none';
    document.getElementById('hash-widget').style.display = 'none';

    const info = document.getElementById('chunk-info');

    if (chunk.tag === 'unit') {
        info.innerHTML = `<h3>unit - Unit Table</h3>
            <p>Offset: 0x${chunk.offset.toString(16)} | Size: ${fmtBytes(chunk.size)}</p>`;
        document.getElementById('unit-table-container').style.display = 'block';
        unitPage = 0;
        loadUnits();
    } else if (chunk.tag === 'hash') {
        info.innerHTML = `<h3>hash - Join Cost Hash Table</h3>
            <p>Offset: 0x${chunk.offset.toString(16)} | Size: ${fmtBytes(chunk.size)}</p>`;
        document.getElementById('hash-widget').style.display = 'block';
        loadHashStats();
    } else if (chunk.tag === 'f0tr' || chunk.tag === 'durt') {
        info.innerHTML = `<h3>${chunk.tag} - CART Tree</h3>
            <p>Offset: 0x${chunk.offset.toString(16)} | Size: ${fmtBytes(chunk.size)}</p>
            <p>Switch to the <strong>Trees</strong> tab for interactive visualization.</p>`;
    } else if (chunk.tag === 'feat') {
        loadFeatDetail(chunk);
    } else {
        loadGenericChunk(chunk);
    }
}

const CHUNK_DESCRIPTIONS = {
    'LIST': 'Container chunk with INFO metadata (copyright, creation date)',
    'vers': 'Engine version string',
    'cnts': 'Unit counts and corpus statistics',
    'feat': 'Feature metadata: recording filenames, phone labels, stored IDs',
    'mean': 'Per-phone feature means for Z-score normalization (duration, pitch, voicing, power)',
    'hash': 'Pre-computed join cost hash table (compressed perfect hash)',
    'ckls': 'Token occurrence streams (word and syllable spans into unit table)',
    'cklx': 'Inverted index from token text to occurrence IDs in ckls',
    'unit': 'Unit table: 169K halfphone units with phone, recording, position, duration, F0',
    'f0tr': 'F0 prediction CART tree (55 leaves, predicts target pitch for WSOLA)',
    'durt': 'Duration CART trees (47 per-phone trees, biases Viterbi unit selection)',
    'ccos': 'Boundary spectral features + duration-continuity cost (join cost fallback on hash miss)',
    'prsl': 'Preselection cache: context trigram -> candidate unit ID lists',
    'hist': 'Z-score histogram for target cost lookup (empirical prior distribution)',
};

function chunkHeader(chunk) {
    const desc = CHUNK_DESCRIPTIONS[chunk.tag] || '';
    return `<h3>${chunk.tag}</h3>
        <p class="chunk-desc">${desc}</p>
        <p class="chunk-meta">Offset: 0x${chunk.offset.toString(16)} | Size: ${fmtBytes(chunk.size)}</p>`;
}

async function loadGenericChunk(chunk) {
    const info = document.getElementById('chunk-info');
    info.innerHTML = chunkHeader(chunk) + '<p style="color:#787882">Loading...</p>';

    if (chunk.tag === 'vers') {
        const data = await apiJson('/api/vin/vers');
        info.innerHTML = chunkHeader(chunk) + `<pre class="chunk-pre">${data.version || '(empty)'}</pre>`;

    } else if (chunk.tag === 'cnts') {
        const data = await apiJson('/api/vin/cnts');
        info.innerHTML = chunkHeader(chunk) +
            `<div class="chunk-grid">
                <div class="chunk-stat"><span class="stat-val">${data.n_units?.toLocaleString()}</span><span class="stat-label">Units</span></div>
                <div class="chunk-stat"><span class="stat-val">${data.val0}</span><span class="stat-label">Val 0</span></div>
                <div class="chunk-stat"><span class="stat-val">${data.val1}</span><span class="stat-label">Val 1</span></div>
            </div>`;

    } else if (chunk.tag === 'mean') {
        const data = await apiJson('/api/vin/mean');
        if (!data) { info.innerHTML = chunkHeader(chunk) + '<p>Parse error</p>'; return; }
        let html = chunkHeader(chunk);
        html += `<p>${data.n_phones} phone variants x ${data.n_features} features</p>`;
        html += `<div class="table-wrap" style="max-height:60vh"><table class="sortable"><thead><tr>
            <th data-sort="string" title="Phone symbol">Phone</th><th data-sort="number">Half</th>
            <th data-sort="number" title="Mean segment duration (ms)">Duration</th>
            <th data-sort="number" title="Duration Z-score normalizer">Dur Z</th>
            <th data-sort="number" title="Mean fundamental frequency (Hz)">Pitch</th>
            <th data-sort="number" title="Pitch Z-score normalizer">Pitch Z</th>
            <th data-sort="number" title="Mean voicing probability (0-1)">Voice</th>
            <th data-sort="number" title="Voicing Z-score normalizer">Voice Z</th>
            <th data-sort="number" title="Mean power (dB-like)">Power</th>
            <th data-sort="number" title="Power Z-score normalizer">Power Z</th>
        </tr></thead><tbody>`;
        data.rows.forEach(r => {
            const isVoiced = r.voice > 0.8;
            html += `<tr>
                <td style="color:${isVoiced ? '#d4a574' : '#8aacb8'}">${r.phone}</td>
                <td data-sort-key="${r.half}">${r.half === 0 ? '1st' : '2nd'}</td>
                <td class="mono" data-sort-key="${r.duration}">${r.duration.toFixed(1)}</td>
                <td class="mono" data-sort-key="${r.dur_z}">${r.dur_z.toFixed(1)}</td>
                <td class="mono" data-sort-key="${r.pitch}">${r.pitch.toFixed(1)}</td>
                <td class="mono" data-sort-key="${r.pitch_z}">${r.pitch_z.toFixed(1)}</td>
                <td class="mono" data-sort-key="${r.voice}">${r.voice.toFixed(3)}</td>
                <td class="mono" data-sort-key="${r.voice_z}">${r.voice_z.toFixed(3)}</td>
                <td class="mono" data-sort-key="${r.power}">${r.power.toFixed(2)}</td>
                <td class="mono" data-sort-key="${r.power_z}">${r.power_z.toFixed(2)}</td>
            </tr>`;
        });
        html += '</tbody></table></div>';
        info.innerHTML = html;

    } else if (chunk.tag === 'hist') {
        const data = await apiJson('/api/vin/hist');
        if (!data) { info.innerHTML = chunkHeader(chunk) + '<p>Parse error</p>'; return; }
        let html = chunkHeader(chunk);
        html += `<p>${data.n_bins} bins | Range: Z-scores ${data.range_start} to ${data.range_start + data.n_bins - 1}</p>`;
        html += `<p>Values are -log P(Z): 0 = most probable, ~11 = rarest (clipped)</p>`;
        // Draw histogram as SVG
        const svgW = 700, svgH = 200, margin = 30;
        const maxVal = Math.max(...data.bins.map(b => b.neg_log_p));
        html += `<svg width="${svgW}" height="${svgH + margin}" style="display:block;margin:12px 0">`;
        const barW = (svgW - margin) / data.n_bins;
        data.bins.forEach((b, i) => {
            const h = (b.neg_log_p / maxVal) * svgH;
            const x = margin + i * barW;
            const hue = (1 - b.neg_log_p / maxVal) * 120; // green=likely, red=rare
            html += `<rect x="${x}" y="${svgH - h}" width="${Math.max(barW-1,1)}" height="${h}"
                     fill="hsl(${hue},50%,45%)" opacity="0.8">
                     <title>Z=${b.z_score} | -log P = ${b.neg_log_p.toFixed(3)}</title></rect>`;
        });
        // Axis labels
        for (let z = -40; z <= 40; z += 10) {
            const x = margin + (z - data.range_start) * barW;
            html += `<text x="${x}" y="${svgH + 14}" fill="#787882" font-size="9" text-anchor="middle">${z}</text>`;
        }
        html += `<text x="${margin/2}" y="${svgH/2}" fill="#787882" font-size="9" transform="rotate(-90,${margin/2},${svgH/2})" text-anchor="middle">-log P</text>`;
        html += '</svg>';
        info.innerHTML = html;

    } else if (chunk.tag === 'prsl') {
        const data = await apiJson('/api/vin/prsl/stats');
        if (!data) { info.innerHTML = chunkHeader(chunk) + '<p>Parse error</p>'; return; }
        let html = chunkHeader(chunk);
        html += `<div class="chunk-grid">
            <div class="chunk-stat"><span class="stat-val">${data.n_groups.toLocaleString()}</span><span class="stat-label">Context groups</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.total_candidates.toLocaleString()}</span><span class="stat-label">Total candidates</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.avg_candidates}</span><span class="stat-label">Avg per group</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.min_candidates}</span><span class="stat-label">Min candidates</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.max_candidates.toLocaleString()}</span><span class="stat-label">Max candidates</span></div>
        </div>`;
        html += `<p style="margin-top:12px;color:#787882">Context key = left_hp * 10000 + center_hp * 100 + right_hp (halfphone trigram)</p>`;
        // Lookup widget
        html += `<div class="inline-form" style="margin-top:12px">
            <label>Left HP: <input id="prsl-left" type="number" style="width:60px" value="0"></label>
            <label>Center HP: <input id="prsl-center" type="number" style="width:60px" value="1"></label>
            <label>Right HP: <input id="prsl-right" type="number" style="width:60px" value="0"></label>
            <button onclick="lookupPrsl()">Lookup</button>
            <span id="prsl-result"></span>
        </div>
        <div id="prsl-candidates"></div>`;
        info.innerHTML = html;

    } else if (chunk.tag === 'ccos') {
        const data = await apiJson('/api/vin/ccos');
        if (!data) { info.innerHTML = chunkHeader(chunk) + '<p>Parse error</p>'; return; }
        let html = chunkHeader(chunk);
        html += `<div class="chunk-grid">
            <div class="chunk-stat"><span class="stat-val">${data.n_phones}</span><span class="stat-label">Phones</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.entries_per_phone}</span><span class="stat-label">Entries/phone</span></div>
            <div class="chunk-stat"><span class="stat-val">${data.floats_per_entry}</span><span class="stat-label">Dims/entry</span></div>
            <div class="chunk-stat"><span class="stat-val">${fmtBytes(data.data_size)}</span><span class="stat-label">Data size</span></div>
        </div>`;
        html += `<p style="margin-top:8px;color:#787882">${data.description}</p>`;
        html += `<p style="margin-top:8px">Phone labels: <span class="mono">${data.labels.join(', ')}</span></p>`;
        info.innerHTML = html;

    } else if (chunk.tag === 'cklx' || chunk.tag === 'ckls') {
        if (chunk.tag === 'cklx') {
            window._cklxChunk = chunk;
            window._cklxGroup = '_WORD_';
            window._cklxPage = 0;
            renderCklx();
        } else {
            info.innerHTML = chunkHeader(chunk) +
                `<p>Token occurrence streams with span values into the unit table.</p>
                 <p>2 groups: _WORD_ (5,108 tokens) and _SYL_ (7,918 tokens)</p>
                 <p>Each token has span_start and span_end (unit table indices) defining its halfphone range.</p>
                 <p>See <strong>cklx</strong> for the reverse lookup index.</p>`;
        }

    } else if (chunk.tag === 'LIST') {
        const data = await apiJson('/api/vin/list_info');
        let html = chunkHeader(chunk);
        if (data && data.fields) {
            const labels = {
                'ICOP': 'Copyright',
                'ICRD': 'Creation Date',
                'INAM': 'Name',
                'IART': 'Artist',
                'ICMT': 'Comment',
                'ISFT': 'Software',
                'IPRD': 'Product',
            };
            html += '<div style="margin-top:8px">';
            for (const [key, val] of Object.entries(data.fields)) {
                const label = labels[key] || key;
                html += `<div style="margin-bottom:6px">
                    <span style="color:var(--accent2);font-size:11px;text-transform:uppercase">${label}</span>
                    <span style="color:var(--text-dim);font-size:10px;margin-left:6px">(${key})</span><br>
                    <span style="font-size:13px">${val}</span>
                </div>`;
            }
            html += '</div>';
        }
        info.innerHTML = html;

    } else {
        info.innerHTML = chunkHeader(chunk) +
            `<p class="placeholder">No detailed viewer for this chunk type.</p>`;
    }
}

let featPage = 0;
const FEAT_PER_PAGE = 50;
let featChunk = null;

async function loadFeatDetail(chunk) {
    featChunk = chunk;
    featPage = 0;
    renderFeatPage();
}

async function renderFeatPage() {
    const info = document.getElementById('chunk-info');
    const q = document.getElementById('feat-search')?.value || '';
    const data = await apiJson(`/api/vin/feat/filenames?page=${featPage}&per_page=${FEAT_PER_PAGE}&q=${encodeURIComponent(q)}`);

    let html = chunkHeader(featChunk);
    html += `<div class="table-controls">
        <input id="feat-search" type="text" placeholder="Search filenames..." value="${q}" style="width:200px">
        <button id="feat-search-btn">Search</button>
        <span style="color:var(--text-dim);font-size:12px">${data.total.toLocaleString()} recordings</span>
    </div>`;

    html += `<div class="table-wrap" style="max-height:55vh"><table class="sortable"><thead><tr>
        <th data-sort="number">ID</th><th data-sort="string">Recording Name</th><th>Go to VDB</th>
    </tr></thead><tbody>`;
    data.items.forEach(item => {
        html += `<tr style="cursor:pointer" onclick="showRecordingForUnit(${item.stored_id})">
            <td class="mono" data-sort-key="${item.stored_id}">${item.stored_id}</td>
            <td>${item.name}</td>
            <td><button class="play-btn" title="View in VDB Explorer">\u279C</button></td>
        </tr>`;
    });
    html += '</tbody></table></div>';

    const totalPages = Math.ceil(data.total / FEAT_PER_PAGE);
    html += `<div class="pagination">
        <button id="feat-prev">Prev</button>
        <span>Page ${featPage + 1} of ${totalPages}</span>
        <button id="feat-next">Next</button>
    </div>`;

    info.innerHTML = html;

    // Wire up controls
    document.getElementById('feat-search-btn')?.addEventListener('click', () => { featPage = 0; renderFeatPage(); });
    document.getElementById('feat-search')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { featPage = 0; renderFeatPage(); } });
    document.getElementById('feat-prev')?.addEventListener('click', () => { if (featPage > 0) { featPage--; renderFeatPage(); } });
    document.getElementById('feat-next')?.addEventListener('click', () => { if (featPage < totalPages - 1) { featPage++; renderFeatPage(); } });
}

// Unit table
async function loadUnits() {
    const phone = document.getElementById('unit-phone-filter').value || undefined;
    const fidxStr = document.getElementById('unit-fidx-filter').value;
    const fidx = fidxStr ? parseInt(fidxStr) : undefined;

    let url = `/api/vin/units?page=${unitPage}&per_page=${UNIT_PER_PAGE}`;
    if (phone) url += `&phone=${phone}`;
    if (fidx !== undefined && !isNaN(fidx)) url += `&fidx=${fidx}`;

    const data = await apiJson(url);
    const tbody = document.getElementById('unit-tbody');
    tbody.innerHTML = '';

    data.items.forEach(u => {
        const tr = document.createElement('tr');
        const recName = u.rec_name || u.file_idx;
        tr.innerHTML = `
            <td data-sort-key="${u.idx}">${u.idx}</td><td data-sort-key="${u.uid}">${u.uid}</td><td>${u.phone}</td><td data-sort-key="${u.half}">${u.half}</td>
            <td title="file_idx: ${u.file_idx}">${recName}</td><td data-sort-key="${u.local_pos}">${u.local_pos}</td><td data-sort-key="${u.dur_like}">${u.dur_like}</td>
            <td data-sort-key="${u.f0_start}">${u.f0_start}</td><td data-sort-key="${u.f0_mid}">${u.f0_mid}</td><td data-sort-key="${u.f0_end}">${u.f0_end}</td>
            <td></td>`;
        // Add play button
        const playTd = tr.lastElementChild;
        playTd.appendChild(playBtn(`/api/vdb/unit_audio/${u.idx}.wav`));
        // Click row to jump to VDB recording
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => {
            showRecordingForUnit(u.file_idx, u.local_pos);
        });
        tbody.appendChild(tr);
    });

    document.getElementById('unit-count').textContent = `${data.total.toLocaleString()} units`;
    const totalPages = Math.ceil(data.total / UNIT_PER_PAGE);
    document.getElementById('unit-page-info').textContent =
        `Page ${data.page + 1} of ${totalPages}`;
}

document.getElementById('unit-prev').addEventListener('click', () => {
    if (unitPage > 0) { unitPage--; loadUnits(); }
});
document.getElementById('unit-next').addEventListener('click', () => {
    unitPage++;
    loadUnits();
});
document.getElementById('unit-filter-btn').addEventListener('click', () => {
    unitPage = 0;
    loadUnits();
});

// Hash widget
async function loadHashStats() {
    const data = await apiJson('/api/vin/hash/stats');
    document.getElementById('hash-stats').innerHTML =
        `<p>Cells: ${data.n_cells?.toLocaleString()} | Rows: ${data.n_rows?.toLocaleString()}</p>`;
}

// cklx viewer with pagination
const CKLX_PER_PAGE = 50;

async function renderCklx() {
    const info = document.getElementById('chunk-info');
    const chunk = window._cklxChunk;
    const group = window._cklxGroup || '_WORD_';
    const page = window._cklxPage || 0;
    const q = document.getElementById('cklx-search')?.value || '';

    const data = await apiJson(`/api/vin/cklx?group=${encodeURIComponent(group)}&page=${page}&per_page=${CKLX_PER_PAGE}&q=${encodeURIComponent(q)}`);
    if (!data) { info.innerHTML = chunkHeader(chunk) + '<p>Parse error</p>'; return; }

    let html = chunkHeader(chunk);

    // Group tabs
    html += `<div class="table-controls">`;
    (data.groups || []).forEach(g => {
        const count = data.group_counts?.[g] || '';
        const active = g === group ? 'style="background:var(--accent2);color:var(--bg);border-color:var(--accent2)"' : '';
        html += `<button ${active} onclick="window._cklxGroup='${g}';window._cklxPage=0;renderCklx()">${g} (${count})</button>`;
    });
    html += `<input id="cklx-search" type="text" placeholder="Search..." value="${q}" style="width:150px;margin-left:auto">
             <button onclick="window._cklxPage=0;renderCklx()">Search</button>`;
    html += `</div>`;

    // Table
    html += `<div class="table-wrap" style="max-height:55vh"><table class="sortable"><thead><tr>
        <th data-sort="string">Key</th><th data-sort="number" title="Number of occurrences in the corpus">Occurrences</th>
    </tr></thead><tbody>`;
    data.items.forEach(e => {
        html += `<tr>
            <td>${e.key}</td>
            <td class="mono" data-sort-key="${e.posting_count}">${e.posting_count}</td>
        </tr>`;
    });
    html += '</tbody></table></div>';

    // Pagination
    const totalPages = Math.ceil(data.total / CKLX_PER_PAGE);
    html += `<div class="pagination">
        <button onclick="if(window._cklxPage>0){window._cklxPage--;renderCklx()}">Prev</button>
        <span>Page ${page + 1} of ${totalPages} (${data.total} entries)</span>
        <button onclick="if(window._cklxPage<${totalPages-1}){window._cklxPage++;renderCklx()}">Next</button>
    </div>`;

    info.innerHTML = html;

    // Wire up Enter key on search
    document.getElementById('cklx-search')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { window._cklxPage = 0; renderCklx(); }
    });
}

// PRSL lookup (called from inline onclick)
async function lookupPrsl() {
    const left = document.getElementById('prsl-left').value;
    const center = document.getElementById('prsl-center').value;
    const right = document.getElementById('prsl-right').value;
    const data = await apiJson(`/api/vin/prsl/lookup?left_hp=${left}&center_hp=${center}&right_hp=${right}`);
    const res = document.getElementById('prsl-result');
    const cands = document.getElementById('prsl-candidates');
    if (data.candidates) {
        res.textContent = `Key: ${data.context_key} | ${data.n_candidates} candidates`;
        cands.innerHTML = `<p class="mono" style="margin-top:8px;font-size:11px;word-break:break-all">UIDs: ${data.candidates.join(', ')}</p>`;
    } else {
        res.textContent = `Key: ${data.context_key} | Not found`;
        cands.innerHTML = '';
    }
}

document.getElementById('hash-lookup-btn').addEventListener('click', async () => {
    const left = document.getElementById('hash-left').value;
    const right = document.getElementById('hash-right').value;
    if (!left || !right) return;
    const data = await apiJson(`/api/vin/hash/lookup?left=${left}&right=${right}`);
    const cost = data.cost !== null ? data.cost.toFixed(6) : 'miss (not in hash)';
    document.getElementById('hash-result').textContent = `Cost: ${cost}`;
});
