"""
Frida session manager for Speechify.exe synthesis tracing.
Handles attach/detach lifecycle and synthesis orchestration.
"""
import os
import time
import threading
import subprocess

try:
    import frida
except ImportError:
    frida = None

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH_EXE = os.path.join(PROJ_ROOT, "bin", "spfy_dumpwav.exe")
SERVER_EXE = os.path.join(PROJ_ROOT, "bin", "Speechify.exe")
HOOK_JS = os.path.join(os.path.dirname(__file__), "viterbi_hook.js")
TARGET = "Speechify.exe"


class FridaManager:
    def __init__(self):
        self.session = None
        self.script = None
        self.attached = False
        self.pid = None
        self._lock = threading.Lock()
        self._synth_results = {}
        self._wsola_results = []
        self._ready = threading.Event()

    def get_state(self):
        return {
            "attached": self.attached,
            "pid": self.pid,
            "frida_available": frida is not None,
        }

    def _is_running(self):
        """Check if Speechify.exe is running."""
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'IMAGENAME eq {TARGET}', '/NH'],
                capture_output=True, text=True, timeout=5
            )
            return TARGET.lower() in result.stdout.lower()
        except Exception:
            return False

    def _start_server(self):
        """Start Speechify.exe server process."""
        if not os.path.exists(SERVER_EXE):
            return {"error": f"Server not found: {SERVER_EXE}"}

        try:
            # Start detached so it doesn't block
            subprocess.Popen(
                [SERVER_EXE],
                cwd=os.path.dirname(SERVER_EXE),
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Wait for it to be ready
            for _ in range(20):  # up to 10 seconds
                time.sleep(0.5)
                if self._is_running():
                    return {"ok": True}
            return {"error": "Speechify.exe started but not responding after 10s"}
        except Exception as e:
            return {"error": f"Failed to start: {e}"}

    def attach(self, auto_start=True):
        if frida is None:
            return {"error": "Frida not installed (pip install frida frida-tools)"}

        with self._lock:
            if self.attached:
                return {"ok": True, "pid": self.pid, "msg": "Already attached"}

            # Auto-start Speechify.exe if not running
            if not self._is_running() and auto_start:
                start_result = self._start_server()
                if not start_result.get("ok"):
                    return start_result

            try:
                self.session = frida.attach(TARGET)
                self.pid = self.session._impl.pid
            except frida.ProcessNotFoundError:
                return {"error": f"{TARGET} not running and auto-start failed."}
            except Exception as e:
                return {"error": str(e)}

            # Load hook JS
            with open(HOOK_JS, "r") as f:
                js_code = f.read()

            self.script = self.session.create_script(js_code)
            self.script.on("message", self._on_message)
            self._ready.clear()
            self.script.load()

            if not self._ready.wait(timeout=5):
                return {"error": "Hook script did not signal ready"}

            self.attached = True
            return {"ok": True, "pid": self.pid}

    def detach(self):
        with self._lock:
            if self.session:
                try:
                    self.session.detach()
                except:
                    pass
            self.session = None
            self.script = None
            self.attached = False
            self.pid = None
            return {"ok": True}

    def _kill_server(self):
        """Kill Speechify.exe if running. Returns True when fully gone."""
        try:
            subprocess.run(
                ['taskkill', '/F', '/IM', TARGET],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        for _ in range(20):  # up to ~6s
            if not self._is_running():
                return True
            time.sleep(0.3)
        return False

    def restart(self):
        """Detach Frida, kill Speechify.exe, re-attach if previously attached.

        Call this after changing tts.voice.name in SWIttsConfig.xml, since the
        engine reads .vin/.vdb only at startup.
        """
        was_attached = False
        with self._lock:
            was_attached = self.attached
            if self.session:
                try:
                    self.session.detach()
                except Exception:
                    pass
            self.session = None
            self.script = None
            self.attached = False
            self.pid = None

        killed = self._kill_server()
        if not killed:
            return {"error": f"Failed to kill {TARGET}"}

        if was_attached:
            return self.attach(auto_start=True)
        return {"ok": True, "attached": False, "killed": True}

    def synthesize(self, text, wav_dir=None):
        """Run synthesis and capture Viterbi trace."""
        if not self.attached:
            return {"error": "Not attached. Call attach first."}

        if wav_dir is None:
            wav_dir = os.path.join(PROJ_ROOT, "viz", "static", "synth_output")
        os.makedirs(wav_dir, exist_ok=True)

        wav_name = f"synth_{int(time.time())}.wav"
        wav_path = os.path.join(wav_dir, wav_name)

        # Clear previous results
        self._synth_results = {}
        self._wsola_results = []

        # Record baseline synth count so we only capture OUR synthesis
        baseline_synth = 0
        try:
            baseline_synth = self.script.exports_sync.get_synth_count()
        except Exception:
            pass

        # Run synthesis with phoneme output
        try:
            result = subprocess.run(
                [SYNTH_EXE, "--phonemes", text, wav_path],
                capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            return {"error": "Synthesis timed out (30s)"}
        except FileNotFoundError:
            return {"error": f"Synth exe not found: {SYNTH_EXE}"}

        if result.returncode != 0:
            return {"error": f"Synthesis failed: {result.stderr.strip()}"}

        # Wait a moment for Frida messages to arrive
        time.sleep(0.5)

        # Collect results -- only from synth calls AFTER our baseline
        pre_prune_hps = []
        for sid in sorted(self._synth_results.keys()):
            if sid <= baseline_synth:
                continue
            pre_prune_hps.extend(self._synth_results[sid].get("hps", []))

        wsola_uids = []
        for wr in self._wsola_results:
            if wr.get("synth", 0) <= baseline_synth:
                continue
            wsola_uids.extend(wr.get("units", []))

        wav_url = f"/static/synth_output/{wav_name}"

        # Parse .phn file for word-to-phoneme mapping
        phn_path = wav_path.replace('.wav', '.phn')
        word_phones = self._parse_phn(phn_path, text)

        return {
            "ok": True,
            "wav_url": wav_url,
            "pre_prune_hps": pre_prune_hps,
            "wsola_uids": wsola_uids,
            "n_hps": len(pre_prune_hps),
            "n_wsola": len(wsola_uids),
            "word_phones": word_phones,
        }

    def _parse_phn(self, phn_path, text):
        """Parse .phn file to extract word -> phone list mapping."""
        words = text.strip().split()
        result = []  # [{word, phones: [{phone, stress}]}]

        try:
            with open(phn_path, 'r') as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []

        current_word = None
        current_phones = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith('# word'):
                # Flush previous word
                if current_word is not None and current_phones:
                    result.append({
                        'word': current_word,
                        'phones': current_phones,
                    })
                    current_phones = []

                # Parse word marker: # word\t<offset>\ttext_off=N\ttext_len=M
                parts = line.split('\t')
                text_off = None
                text_len = None
                for p in parts:
                    if p.startswith('text_off='):
                        text_off = int(p.split('=')[1])
                    elif p.startswith('text_len='):
                        text_len = int(p.split('=')[1])

                if text_off is not None and text_len is not None:
                    current_word = text[text_off:text_off + text_len]
                else:
                    current_word = f'[word]'

            elif line.startswith('#'):
                continue  # skip other comments

            else:
                # Phone line: start\tend\tphone\tstress
                parts = line.split('\t')
                if len(parts) >= 3:
                    phone = parts[2]
                    stress = int(parts[3]) if len(parts) >= 4 else 0
                    current_phones.append({'phone': phone, 'stress': stress})

        # Flush last word
        if current_word is not None and current_phones:
            result.append({
                'word': current_word,
                'phones': current_phones,
            })

        return result

    def _on_message(self, message, data):
        if message["type"] == "send":
            payload = message["payload"]
            t = payload.get("type")
            if t == "ready":
                self._ready.set()
            elif t == "synth_done":
                self._synth_results[payload["synth"]] = payload
            elif t == "wsola":
                self._wsola_results.append(payload)
        elif message["type"] == "error":
            print(f"  [Frida ERROR] {message.get('stack', message)}")
