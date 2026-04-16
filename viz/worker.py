"""
Speechify Synthesis Worker - runs on the Windows TTS machine.
Provides HTTP API for synthesis with optional Frida trace capture.

Endpoints:
  POST /synth
    Body: {"text": "...", "voice": "tom"}
    Params: ?trace=1  (enable Frida trace capture)
    Returns:
      - Without trace: WAV file (audio/wav)
      - With trace: JSON {wav_b64, pre_prune_hps, wsola_uids, word_phones, ...}

  GET /status
    Returns: {"ready": true, "busy": false, "voices": [...]}

Run: python viz/worker.py --port 5001 --host 0.0.0.0
"""
import argparse
import base64
import json
import os
import sys
import time
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add project root
PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_ROOT)

SYNTH_EXE = os.path.join(PROJ_ROOT, "bin", "spfy_dumpwav.exe")
SERVER_EXE = os.path.join(PROJ_ROOT, "bin", "Speechify.exe")
WAV_DIR = os.path.join(PROJ_ROOT, "viz", "worker_output")
os.makedirs(WAV_DIR, exist_ok=True)

# Auth token (set via --token or SYNTH_WORKER_TOKEN env)
AUTH_TOKEN = None

# Synthesis lock (one at a time)
_synth_lock = threading.Lock()
_busy = False

# Lazy Frida manager (only imported when trace mode is used)
_frida_mgr = None


def get_frida_mgr():
    global _frida_mgr
    if _frida_mgr is None:
        from viz.frida_hooks.manager import FridaManager
        _frida_mgr = FridaManager()
    return _frida_mgr


def list_voices():
    """Auto-discover voices from en-US/*/."""
    voice_root = os.path.join(PROJ_ROOT, "en-US")
    voices = []
    if os.path.exists(voice_root):
        for d in sorted(os.listdir(voice_root)):
            vin = os.path.join(voice_root, d, f"{d}.vin")
            if os.path.exists(vin):
                voices.append(d)
    return voices


def synthesize_plain(text, voice="tom"):
    """Plain synthesis: just return WAV bytes."""
    wav_name = f"synth_{int(time.time()*1000)}.wav"
    wav_path = os.path.join(WAV_DIR, wav_name)

    result = subprocess.run(
        [SYNTH_EXE, text, wav_path],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None, f"Synthesis failed: {result.stderr.strip()}"

    with open(wav_path, 'rb') as f:
        wav_bytes = f.read()

    try:
        os.remove(wav_path)
    except:
        pass

    return wav_bytes, None


def synthesize_traced(text, voice="tom"):
    """Traced synthesis: Frida capture + phoneme output."""
    mgr = get_frida_mgr()

    # Auto-attach if needed
    if not mgr.attached:
        result = mgr.attach(auto_start=True)
        if not result.get("ok"):
            return None, f"Frida attach failed: {result.get('error')}"

    result = mgr.synthesize(text, wav_dir=WAV_DIR)
    if not result.get("ok"):
        return None, result.get("error", "Synthesis failed")

    # Read the WAV file
    wav_url = result.get("wav_url", "")
    wav_path = os.path.join(PROJ_ROOT, "viz", "static",
                            wav_url.lstrip("/static/")) if wav_url else None

    # Try worker_output path
    if wav_path and not os.path.exists(wav_path):
        # The manager may have written to a different path
        for fname in sorted(os.listdir(WAV_DIR), reverse=True):
            if fname.endswith('.wav'):
                wav_path = os.path.join(WAV_DIR, fname)
                break

    wav_b64 = None
    if wav_path and os.path.exists(wav_path):
        with open(wav_path, 'rb') as f:
            wav_b64 = base64.b64encode(f.read()).decode('ascii')

    return {
        "ok": True,
        "wav_b64": wav_b64,
        "pre_prune_hps": result.get("pre_prune_hps", []),
        "wsola_uids": result.get("wsola_uids", []),
        "word_phones": result.get("word_phones", []),
        "n_hps": result.get("n_hps", 0),
        "n_wsola": result.get("n_wsola", 0),
    }, None


class WorkerHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        if AUTH_TOKEN is None:
            return True
        token = self.headers.get('Authorization', '').replace('Bearer ', '')
        if token == AUTH_TOKEN:
            return True
        self._respond(401, {"error": "Unauthorized"})
        return False

    def _respond(self, code, data, content_type='application/json'):
        self.send_response(code)
        if content_type == 'application/json':
            body = json.dumps(data).encode()
            self.send_header('Content-Type', 'application/json')
        else:
            body = data
            self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/status':
            self._respond(200, {
                "ready": True,
                "busy": _busy,
                "voices": list_voices(),
            })
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path != '/synth':
            self._respond(404, {"error": "Not found"})
            return

        # Read body
        content_len = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
        text = body.get('text', '')
        voice = body.get('voice', 'tom')
        trace = '1' in params.get('trace', [])

        if not text:
            self._respond(400, {"error": "No text provided"})
            return

        global _busy
        if not _synth_lock.acquire(timeout=0.1):
            self._respond(503, {"error": "Busy - synthesis in progress"})
            return

        try:
            _busy = True
            if trace:
                result, err = synthesize_traced(text, voice)
                if err:
                    self._respond(500, {"error": err})
                else:
                    self._respond(200, result)
            else:
                wav_bytes, err = synthesize_plain(text, voice)
                if err:
                    self._respond(500, {"error": err})
                else:
                    self._respond(200, wav_bytes, content_type='audio/wav')
        except Exception as e:
            self._respond(500, {"error": str(e)})
        finally:
            _busy = False
            _synth_lock.release()

    def log_message(self, format, *args):
        print(f"  [{time.strftime('%H:%M:%S')}] {args[0]}")


def main():
    global AUTH_TOKEN
    parser = argparse.ArgumentParser(description="Speechify Synthesis Worker")
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--token', default=os.environ.get('SYNTH_WORKER_TOKEN'),
                        help='Auth token (or set SYNTH_WORKER_TOKEN env)')
    args = parser.parse_args()

    AUTH_TOKEN = args.token

    print("=" * 50)
    print("  Speechify Synthesis Worker")
    print("=" * 50)
    print(f"  Project: {PROJ_ROOT}")
    print(f"  Voices: {list_voices()}")
    print(f"  Auth: {'enabled' if AUTH_TOKEN else 'disabled'}")
    print(f"  Listening: http://{args.host}:{args.port}")
    print("=" * 50)

    server = HTTPServer((args.host, args.port), WorkerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == '__main__':
    main()
