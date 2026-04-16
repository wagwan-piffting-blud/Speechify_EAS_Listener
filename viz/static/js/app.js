/* Speechify Visualizer - Main app (tab switching, fetch helpers, voice selector) */

const API = '';  // same origin

async function api(path, opts) {
    const voice = document.getElementById('voice-select').value || 'tom';
    const sep = path.includes('?') ? '&' : '?';
    const url = `${API}${path}${sep}voice=${voice}`;
    const resp = await fetch(url, opts);
    if (!resp.ok) throw new Error(`API error: ${resp.status}`);
    return resp;
}

async function apiJson(path) {
    const resp = await api(path);
    return resp.json();
}

// Tab switching with URL hash routing
function switchTab(tabId, pushState) {
    document.querySelectorAll('#tabs .tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    const btn = document.querySelector(`[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');
    const panel = document.getElementById(tabId);
    if (panel) panel.classList.add('active');

    if (pushState !== false) {
        const hash = '#' + tabId;
        if (location.hash !== hash) history.pushState(null, '', hash);
    }

    // Trigger load for the tab if needed
    if (tabId === 'vin-explorer' && !window._vinLoaded) loadVinExplorer();
    if (tabId === 'vdb-explorer' && !window._vdbLoaded) loadVdbExplorer();
    if (tabId === 'trees' && !window._treesLoaded) loadTrees();
}

document.querySelectorAll('#tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// Handle back/forward navigation
window.addEventListener('popstate', () => {
    const tab = location.hash.replace('#', '') || 'vin-explorer';
    switchTab(tab, false);
});

// Restore tab from URL hash on load
function restoreFromHash() {
    const hash = location.hash.replace('#', '');
    if (hash && document.getElementById(hash)) {
        switchTab(hash, false);
    }
}

// Voice selector
async function loadVoices() {
    const voices = await apiJson('/api/voices');
    const sel = document.getElementById('voice-select');
    const info = document.getElementById('voice-info');
    sel.innerHTML = '';
    voices.forEach(v => {
        const opt = document.createElement('option');
        opt.value = v.name;
        opt.textContent = v.name;
        sel.appendChild(opt);
    });

    // Sync initial selection with whatever SWIttsConfig.xml currently points at
    let current = null;
    try {
        const cur = await fetch('/api/voices/current').then(r => r.json());
        current = cur && cur.voice;
    } catch (_) { /* ignore */ }
    if (current && sel.querySelector(`option[value="${current}"]`)) {
        sel.value = current;
    } else if (sel.querySelector('option[value="tom"]')) {
        sel.value = 'tom';
    }
    sel.dataset.lastValue = sel.value;
    if (info) info.textContent = `active: ${sel.value}`;

    sel.addEventListener('change', async () => {
        const newVoice = sel.value;
        const prev = sel.dataset.lastValue || 'tom';
        sel.disabled = true;
        if (info) info.textContent = `switching to ${newVoice}\u2026`;
        AudioMgr.stopAll();
        try {
            const resp = await fetch('/api/voices/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newVoice }),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            sel.dataset.lastValue = newVoice;
            if (info) {
                const restarted = data.restart && data.restart.attached;
                info.textContent = restarted
                    ? `active: ${newVoice} (Speechify re-attached)`
                    : `active: ${newVoice}`;
            }
        } catch (e) {
            if (info) info.textContent = `error: ${e.message}`;
            sel.value = prev;  // revert dropdown
            sel.disabled = false;
            return;
        }
        sel.disabled = false;

        // Reset loaded flags and reload current tab
        window._vinLoaded = false;
        window._vdbLoaded = false;
        window._treesLoaded = false;
        const active = document.querySelector('#tabs .tab.active');
        if (active) active.click();
    });
}

// Helper: format bytes
function fmtBytes(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
}

// Global audio manager: single audio context, play/pause/stop across all
const AudioMgr = {
    _current: null,      // {audio, btn, onStop}
    _allBtns: new Set(),

    stopAll() {
        if (this._current) {
            this._current.audio.pause();
            this._current.audio.currentTime = 0;
            this._current.btn.textContent = '\u25B6';
            if (this._current.onStop) this._current.onStop();
            this._current = null;
        }
        // Also stop any stray <audio> elements
        document.querySelectorAll('audio').forEach(a => { a.pause(); a.currentTime = 0; });
        this._allBtns.forEach(b => { b.textContent = '\u25B6'; });
    },

    play(url, btn, onStop) {
        // If clicking the currently playing button, pause/resume
        if (this._current && this._current.btn === btn) {
            if (this._current.audio.paused) {
                this._current.audio.play();
                btn.textContent = '\u23F8';  // pause icon
                return;
            } else {
                this._current.audio.pause();
                btn.textContent = '\u25B6';
                return;
            }
        }

        // Stop whatever else is playing
        this.stopAll();

        const voice = document.getElementById('voice-select').value || 'tom';
        const fullUrl = url + (url.includes('?') ? '&' : '?') + 'voice=' + voice;
        const audio = new Audio(fullUrl);

        this._current = { audio, btn, onStop };
        btn.textContent = '\u23F8';  // pause icon

        audio.play();
        audio.addEventListener('ended', () => {
            btn.textContent = '\u25B6';
            if (onStop) onStop();
            this._current = null;
        });
        audio.addEventListener('error', () => {
            btn.textContent = '\u25B6';
            this._current = null;
        });
    },

    // For managed <audio> elements (VDB explorer, synth tracer)
    playManaged(audioEl, btn) {
        if (this._current && this._current.btn === btn) {
            if (audioEl.paused) {
                audioEl.play();
                btn.textContent = '\u23F8';
            } else {
                audioEl.pause();
                btn.textContent = '\u25B6';
            }
            return;
        }

        this.stopAll();
        this._current = { audio: audioEl, btn, onStop: null };
        btn.textContent = '\u23F8';
        audioEl.play();
        audioEl.addEventListener('ended', () => {
            btn.textContent = '\u25B6';
            this._current = null;
        }, { once: true });
    }
};

// Stop all audio on tab switch
document.querySelectorAll('#tabs .tab').forEach(btn => {
    btn.addEventListener('click', () => AudioMgr.stopAll(), true);
});

// Helper: create audio play button (uses global AudioMgr)
function playBtn(url) {
    const btn = document.createElement('button');
    btn.className = 'play-btn';
    btn.textContent = '\u25B6';
    btn.title = 'Play';
    AudioMgr._allBtns.add(btn);
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        AudioMgr.play(url, btn);
    });
    return btn;
}

// Sortable tables: click any <th> with data-sort to sort its table
// data-sort="string|number" on <th>, data-sort-key="value" on <td>
function makeSortable(table) {
    const headers = table.querySelectorAll('th[data-sort]');
    headers.forEach(th => {
        th.style.cursor = 'pointer';
        th.addEventListener('click', () => {
            const colIdx = Array.from(th.parentNode.children).indexOf(th);
            const tbody = table.querySelector('tbody');
            if (!tbody) return;
            const rows = Array.from(tbody.querySelectorAll('tr:not(.row-candidates)'));
            const type = th.dataset.sort; // 'string' or 'number'
            const asc = th.dataset.sortDir !== 'asc';
            th.dataset.sortDir = asc ? 'asc' : 'desc';

            // Clear other sort indicators
            headers.forEach(h => { if (h !== th) { h.dataset.sortDir = ''; h.classList.remove('sort-asc', 'sort-desc'); } });
            th.classList.toggle('sort-asc', asc);
            th.classList.toggle('sort-desc', !asc);

            rows.sort((a, b) => {
                const cellA = a.children[colIdx];
                const cellB = b.children[colIdx];
                if (!cellA || !cellB) return 0;
                let va = cellA.dataset.sortKey !== undefined ? cellA.dataset.sortKey : cellA.textContent.trim();
                let vb = cellB.dataset.sortKey !== undefined ? cellB.dataset.sortKey : cellB.textContent.trim();
                if (type === 'number') {
                    va = parseFloat(va) || 0;
                    vb = parseFloat(vb) || 0;
                    return asc ? va - vb : vb - va;
                }
                return asc ? va.localeCompare(vb) : vb.localeCompare(va);
            });

            rows.forEach(r => tbody.appendChild(r));
        });
    });
}

// Auto-init sortable tables after DOM mutations
const _sortObserver = new MutationObserver(() => {
    document.querySelectorAll('table.sortable:not([data-sort-init])').forEach(t => {
        t.dataset.sortInit = '1';
        makeSortable(t);
    });
});
_sortObserver.observe(document.body, { childList: true, subtree: true });

// Boot
loadVoices().then(() => {
    restoreFromHash();
    // If no hash, default to vin-explorer
    if (!location.hash) loadVinExplorer();
});
