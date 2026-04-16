/* VDB Explorer - recording list + waveform with unit overlays + audio playback */

let recPage = 0;
const REC_PER_PAGE = 50;
let currentRecUnits = null;
let recSort = 'name';
let recSortDir = 'asc';

async function loadVdbExplorer() {
    window._vdbLoaded = true;
    loadRecordings();
}

let wordSearchResults = null;

async function loadRecordings() {
    const q = document.getElementById('rec-search').value || '';
    const ul = document.getElementById('rec-list');

    // If query looks like a word (not a recording name pattern), search word index too
    if (q && !q.includes('_') && q.length >= 2) {
        try {
            const wordData = await apiJson(`/api/vin/search_words?q=${encodeURIComponent(q)}`);
            if (wordData.matches && wordData.matches.length > 0) {
                wordSearchResults = wordData;
                renderWordSearchResults(wordData, q);
                return;
            }
        } catch(e) {}
    }

    wordSearchResults = null;

    const data = await apiJson(`/api/vdb/recordings?page=${recPage}&per_page=${REC_PER_PAGE}&q=${encodeURIComponent(q)}&sort=${recSort}&sort_dir=${recSortDir}`);

    ul.innerHTML = '';
    data.items.forEach(rec => {
        const li = document.createElement('li');
        li.innerHTML = `<span>${rec.name}</span><span class="dur">${(rec.duration_ms/1000).toFixed(1)}s</span>`;
        li.dataset.name = rec.name;
        li.dataset.index = rec.index;
        li.addEventListener('click', () => selectRecording(rec, li));
        ul.appendChild(li);
    });

    const totalPages = Math.ceil(data.total / REC_PER_PAGE);
    document.getElementById('rec-page-info').textContent =
        `Page ${data.page + 1} of ${totalPages} (${data.total} recordings)`;
}

function renderWordSearchResults(wordData, query) {
    const ul = document.getElementById('rec-list');
    ul.innerHTML = '';

    // Collect all unique recordings from all word matches
    const recSet = new Map();
    wordData.matches.forEach(m => {
        (m.recordings || []).forEach(recName => {
            if (!recSet.has(recName)) {
                recSet.set(recName, []);
            }
            recSet.get(recName).push(m.word);
        });
    });

    // Header showing word matches
    const header = document.createElement('li');
    header.style.color = 'var(--accent)';
    header.style.fontSize = '11px';
    header.style.padding = '6px 8px';
    header.style.cursor = 'default';
    const wordList = wordData.matches.map(m => m.word).slice(0, 10).join(', ');
    header.innerHTML = `<span>Words: ${wordList}${wordData.matches.length > 10 ? '...' : ''}</span>`;
    ul.appendChild(header);

    // List recordings
    recSet.forEach((words, recName) => {
        const li = document.createElement('li');
        li.innerHTML = `<span>${recName}<br><small style="color:var(--text-dim)">${words.join(', ')}</small></span>`;
        li.dataset.name = recName;
        li.addEventListener('click', async () => {
            // Fetch recording info and select it
            const allRecs = await apiJson(`/api/vdb/recordings?q=${encodeURIComponent(recName)}&page=0&per_page=5`);
            const rec = allRecs.items.find(r => r.name === recName);
            if (rec) selectRecording(rec, li);
        });
        ul.appendChild(li);
    });

    document.getElementById('rec-page-info').textContent =
        `${recSet.size} recordings containing "${query}"`;
}

async function selectRecording(rec, li) {
    document.querySelectorAll('#rec-list li').forEach(l => l.classList.remove('active'));
    if (li) li.classList.add('active');

    const info = document.getElementById('rec-info');
    info.innerHTML = `<h3>${rec.name}</h3>
        <p>Index: ${rec.index} | Duration: ${(rec.duration_ms/1000).toFixed(2)}s | Size: ${fmtBytes(rec.size)} | Offset: 0x${rec.offset.toString(16)}</p>`;

    // Audio player with waveform cursor
    const audioDiv = document.getElementById('rec-audio');
    audioDiv.innerHTML = '';
    const voice = document.getElementById('voice-select').value || 'tom';
    const audioUrl = `/api/vdb/audio/${rec.name}.wav?voice=${voice}`;

    const audio = document.createElement('audio');
    audio.src = audioUrl;
    audio.style.display = 'none';
    audioDiv.appendChild(audio);

    const pb = document.createElement('button');
    pb.textContent = '\u25B6 Play';
    pb.style.marginRight = '8px';
    AudioMgr._allBtns.add(pb);
    pb.addEventListener('click', () => {
        AudioMgr.playManaged(audio, pb);
    });
    audio.addEventListener('ended', () => { pb.textContent = '\u25B6 Play'; });
    audioDiv.appendChild(pb);

    // Load waveform + unit boundaries + tokens
    const [wfData, units, tokens] = await Promise.all([
        apiJson(`/api/vdb/waveform/${rec.name}`),
        apiJson(`/api/vin/units_for_recording/${rec.name}`),
        apiJson(`/api/vin/recording_tokens/${rec.name}`)
    ]);

    currentRecUnits = units;
    drawWaveformWithUnits(
        document.getElementById('waveform-canvas'),
        wfData.samples,
        wfData.total_samples,
        units,
        rec.name,
        tokens
    );

    // Now that waveform is drawn, attach playback cursor
    attachWaveformCursor(audio);

    // Show word/syllable tokens below waveform
    const tokenDiv = document.getElementById('rec-tokens') || (() => {
        const d = document.createElement('div');
        d.id = 'rec-tokens';
        document.getElementById('rec-detail').appendChild(d);
        return d;
    })();

    if (tokens.words.length > 0 || tokens.syllables.length > 0) {
        let html = '<div class="token-section">';
        if (tokens.words.length > 0) {
            html += '<div class="token-group"><span class="token-label">Words:</span>';
            tokens.words.forEach(w => {
                html += `<span class="token-word" title="units ${w.span_start}-${w.span_end}">${w.text}</span>`;
            });
            html += '</div>';
        }
        if (tokens.syllables.length > 0) {
            html += '<div class="token-group"><span class="token-label">Syllables:</span>';
            tokens.syllables.forEach(s => {
                html += `<span class="token-syl" title="units ${s.span_start}-${s.span_end}">${s.text}</span>`;
            });
            html += '</div>';
        }
        html += '</div>';
        tokenDiv.innerHTML = html;
    } else {
        tokenDiv.innerHTML = '<p style="color:#606068;font-size:11px;margin-top:8px">No word/syllable tokens for this recording.</p>';
    }
}

// Cross-link: called from VIN Explorer when clicking a unit row
async function showRecordingForUnit(fileIdx, highlightLp) {
    switchTab('vdb-explorer');
    if (!window._vdbLoaded) await loadVdbExplorer();

    // Get recording info for this file_idx
    const data = await apiJson(`/api/vdb/recordings?page=0&per_page=1&q=`);
    // We need to find the recording by index. Fetch all and filter.
    // Actually, the file_idx maps to the recording index
    const allRecs = await apiJson(`/api/vdb/recordings?page=${Math.floor(fileIdx / REC_PER_PAGE)}&per_page=${REC_PER_PAGE}`);
    const rec = allRecs.items.find(r => r.index === fileIdx);

    if (rec) {
        // Find and highlight in the list
        const li = document.querySelector(`#rec-list li[data-index="${fileIdx}"]`);
        selectRecording(rec, li);
    }
}

document.getElementById('rec-search').addEventListener('input', () => {
    recPage = 0;
    loadRecordings();
});

// Sort buttons
document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const newSort = btn.dataset.sort;
        if (recSort === newSort) {
            recSortDir = recSortDir === 'asc' ? 'desc' : 'asc';
        } else {
            recSort = newSort;
            recSortDir = 'asc';
        }
        document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        btn.textContent = btn.dataset.sort.charAt(0).toUpperCase() + btn.dataset.sort.slice(1) +
                          (recSortDir === 'asc' ? ' \u25B2' : ' \u25BC');
        recPage = 0;
        loadRecordings();
    });
});
document.getElementById('rec-prev').addEventListener('click', () => {
    if (recPage > 0) { recPage--; loadRecordings(); }
});
document.getElementById('rec-next').addEventListener('click', () => {
    recPage++;
    loadRecordings();
});
