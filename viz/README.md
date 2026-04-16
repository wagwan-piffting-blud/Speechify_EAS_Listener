# Speechify VIN/VDB Visualizer

A web-based tool for poking around the internals of SpeechWorks Speechify 3.0.5
voice files (`*.vin` / `*.vdb`) and watching the unit-selection engine do its
thing in real time. HTML/CSS/JS frontend, light Flask backend, optional Frida
integration for live synthesis tracing.

## What's in it

Four tabs, all sharing a voice selector in the top bar:

1. **VIN Explorer** — every chunk in the `.vin` file gets a dedicated viewer:
   - Unit table with pagination, phone filtering, and sortable columns
   - `feat` filename list (clickable, cross-links to VDB Explorer)
   - `mean` / `hist` statistics
   - `prsl` context-key lookup widget
   - `hash` lookup (join-cost hash table)
   - `ccos` summary
   - `cklx` word/syllable entries with group tabs and pagination
   - `ckls` file-id index
   - `LIST`/`INFO` metadata
2. **Trees** — SVG renderer for the `f0tr` (pitch) and `durt` (duration) CART
   trees. Leaf-count-weighted layout, viewBox pan/zoom, double-click to reset.
3. **VDB Explorer** — recording browser. Word search, sortable by
   name/duration/index, waveform view with phone-colored regions, word brackets,
   and a playback cursor.
4. **Synthesis Tracer** — type a sentence, watch the engine pick units.
   Timeline, waveform overlay, collapsible per-word halfphone detail tables,
   recording usage chart, per-candidate pruning flags.

Every tool has its own URL hash (`#vin`, `#trees`, `#vdb`, `#tracer`) so you
can bookmark a tab.

## Quick start (local / Windows dev machine)

```bash
pip install -r viz/requirements.txt
python viz/run_viz.py
```

Then open <http://localhost:5000>.

Voices are auto-discovered from `en-US/*/`. The default is `tom`. Switch voices
with the dropdown in the top bar.

Requirements:

- **Python 3.10+** (3.12 is what I use)
- **Flask 3.0+**
- **Frida** (Windows only, for the tracer — the rest works without it)
- The `en-US/` voice directory from the parent repo (e.g. `en-US/tom/tom.vin`
  and `en-US/tom/tom8.vdb`)

If Frida isn't installed, tabs 1–3 work fine; the Synthesis Tracer will tell
you it's unavailable.

## Synthesis tracing mode

Runs on the same Windows box as Speechify. Click **Attach** in the Tracer tab
and it will:

1. Start `bin/Speechify.exe` if it isn't already running
2. Attach Frida and inject `viterbi_hook.js`
3. Hook the prune function, USEL entry, and WSOLA concat entry
4. Use `bin/spfy_dumpwav.exe` for the actual synthesis

Each trace does a fresh detach/reattach to isolate results from any other
synthesis happening on the same Speechify process (e.g. a balcon job in
another window).

## Layout

```
viz/
├── app.py                  Flask app: all API routes, voice loading, worker proxy
├── run_viz.py              Entry point (port 5000, host 0.0.0.0)
├── requirements.txt        flask, frida (win32)
├── worker.py               Standalone worker (superseded by monolith integration)
├── parsers/
│   ├── vin_parser.py       VIN RIFF parsing + all chunk decoders
│   └── vdb_parser.py       VDB indx + u-law audio extraction
├── frida_hooks/
│   ├── manager.py          FridaManager: attach/detach/synthesize lifecycle
│   └── viterbi_hook.js     Interceptor payload (prune / USEL / WSOLA hooks)
└── static/
    ├── index.html          SPA shell, tab nav, voice selector
    ├── css/main.css        Dark theme (warm muted palette)
    └── js/
        ├── app.js          Tab routing, fetch helpers, AudioMgr, sortable tables
        ├── vin-explorer.js
        ├── vdb-explorer.js
        ├── tree-viz.js
        ├── synth-tracer.js
        └── waveform.js
```

No build step. Edit any file under `static/` and refresh the browser — the
Flask server serves `static/` with `Cache-Control: no-store` so hot-reload
just works.

## Troubleshooting

- **"Frida not available"** — `pip install frida` on the machine running the
  Flask app (or the remote worker, in remote mode).
- **Tracer shows results from a different synthesis** — make sure you're on a
  build that does the fresh detach/reattach per request. Concurrent jobs on
  `Speechify.exe` will otherwise contaminate the hook output.
- **404 on recording audio** — the VDB positional index is *not* the same as
  the VIN `feat` stored_id. All lookups go through recording *name*; if you
  see 404s, you're probably hitting an old code path that used the raw index.
- **VDB explorer shows wrong data for a recording** — double-check the voice
  dropdown. Voice data is cached per-name in `_voice_data`; restart the server
  if you've rebuilt a `.vin`/`.vdb` while it was running.

## Tips

- Every table header is sortable — click to toggle asc/desc.
- Every Play button uses a shared `AudioMgr` so starting one clip stops the
  previous one. The gold playback cursor stays on the waveform through
  pause/resume.
- URL hashes (`#vin`, `#trees`, `#vdb`, `#tracer`) are deep-linkable. Tools
  restore their last-viewed state on reload where it's cheap to do so.
- In the Tracer, collapse word groups you don't care about to keep the
  per-halfphone detail table manageable on long sentences.
