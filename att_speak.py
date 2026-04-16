"""
att_speak.py -- AT&T Natural Voices 5.1 TTS with inline pitch control

Usage:
    python att_speak.py "Hello world" out.wav
    python att_speak.py "Hello \\!PITCH=1.3 world" out.wav
    python att_speak.py -f script.txt out.wav
    python att_speak.py -v crystal16 "Hello world" out.wav
    python att_speak.py --pitch 1.3 "Hello world" out.wav

Pitch control (via Frida hook):
    Inline:   \\!PITCH=1.3  in the text (30% higher, per-phrase)
    Global:   --pitch 1.3  flag (applies to entire utterance)
    Values:   >1.0 = higher pitch, <1.0 = lower pitch, 1.0 = normal
    Duration changes proportionally; combine with \\!SPEED= to compensate.
"""
import argparse
import os
import subprocess
import sys
import tempfile

ATT_ROOT = os.path.join(os.environ.get('APPDATA', ''),
                        'ATTNaturalVoices', 'TTS5.1')
ATT_BIN = os.path.join(ATT_ROOT, 'bin')
EXE = 'TTSStandaloneFile_32.exe'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK_JS = os.path.join(SCRIPT_DIR, 'att_pitch_fix.js')


def speak(text=None, text_file=None, output='out.wav', voice='mike16',
          pitch=None, verbose=False):
    """Synthesize speech, optionally with pitch control.

    Returns the output file path on success.
    """
    # -- Resolve input text -----------------------------------------------
    cleanup_input = False
    if text_file:
        input_path = os.path.abspath(text_file)
    elif text:
        fd, input_path = tempfile.mkstemp(suffix='.txt', dir=os.environ.get('TMP', '.'))
        with os.fdopen(fd, 'w') as f:
            # If global --pitch given, wrap entire text
            if pitch is not None and pitch != 1.0:
                f.write('\\!PITCH=%.4g %s' % (pitch, text))
            else:
                f.write(text)
        cleanup_input = True
    else:
        raise ValueError('provide text or text_file')

    output_path = os.path.abspath(output)

    # -- Decide whether Frida is needed -----------------------------------
    # Read the input to check for pitch escapes
    with open(input_path, 'r') as f:
        content = f.read()
    needs_frida = ('!PITCH=' in content)

    # -- Build exe arguments ----------------------------------------------
    exe_args = [
        '-f', input_path,
        '-o', output_path,
        '-data', '..\\data',
        '-root', '.',
        '-config', '..\\data\\tts.cfg',
        '-x', voice,
    ]
    if verbose:
        exe_args.insert(0, '-v3')

    # -- Run --------------------------------------------------------------
    try:
        if needs_frida:
            _run_with_frida(exe_args, verbose)
        else:
            _run_direct(exe_args, verbose)
    finally:
        if cleanup_input:
            try:
                os.unlink(input_path)
            except OSError:
                pass

    if not os.path.exists(output_path):
        print('Error: no output file generated', file=sys.stderr)
        return None
    return output_path


def _run_direct(exe_args, verbose):
    """Run the TTS exe without Frida (no pitch control needed)."""
    cmd = [os.path.join(ATT_BIN, EXE)] + exe_args
    kw = dict(cwd=ATT_BIN)
    if not verbose:
        kw['stdout'] = subprocess.DEVNULL
        kw['stderr'] = subprocess.DEVNULL
    subprocess.run(cmd, **kw)


def _run_with_frida(exe_args, verbose):
    """Run the TTS exe under Frida with the pitch hook."""
    # Try Python frida API first (cleaner), fall back to CLI
    try:
        import frida as frida_mod
        _run_frida_api(frida_mod, exe_args, verbose)
        return
    except ImportError:
        pass
    except Exception as e:
        if verbose:
            print('frida API failed (%s), trying CLI...' % e, file=sys.stderr)

    _run_frida_cli(exe_args, verbose)


def _run_frida_api(frida_mod, exe_args, verbose):
    """Use the frida Python API to spawn + hook."""
    import threading

    exe_path = os.path.join(ATT_BIN, EXE)
    with open(HOOK_JS, 'r') as f:
        hook_source = f.read()

    device = frida_mod.get_local_device()
    pid = device.spawn([exe_path] + exe_args, cwd=ATT_BIN)
    session = device.attach(pid)

    done = threading.Event()

    def on_detached(reason, crash):
        done.set()

    def on_message(message, data):
        if verbose and message.get('type') == 'send':
            print(message.get('payload', ''))

    session.on('detached', on_detached)
    script = session.create_script(hook_source)
    script.on('message', on_message)
    script.load()
    device.resume(pid)
    done.wait(timeout=60)


def _run_frida_cli(exe_args, verbose):
    """Fall back to the frida CLI tool."""
    cmd = ['frida', '-f', EXE, '-l', HOOK_JS, '--'] + exe_args
    proc = subprocess.Popen(
        cmd, cwd=ATT_BIN,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if not verbose else None,
        stderr=subprocess.PIPE if not verbose else None,
    )
    try:
        proc.communicate(input=b'', timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


# -- CLI ------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='AT&T Natural Voices 5.1 TTS with pitch control',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Inline pitch (per-phrase):
    "Hello \\!PITCH=1.3 world"      -- only "world" is pitched up
    "\\!PITCH=0.7 deep voice here"  -- everything after the tag

Global pitch:
    --pitch 1.3 "Hello world"      -- entire utterance pitched up

Values:  1.3 = 30%% higher,  0.7 = 30%% lower,  1.0 = normal
Duration scales as 1/factor. Add \\!SPEED=N to compensate if needed.
""")
    p.add_argument('text', nargs='?', help='Text to synthesize')
    p.add_argument('output', help='Output WAV path')
    p.add_argument('-f', '--file', help='Read text from file instead')
    p.add_argument('-v', '--voice', default='mike16',
                   help='Voice name (default: mike16)')
    p.add_argument('-p', '--pitch', type=float, default=None,
                   help='Global pitch factor (1.3=higher, 0.7=lower)')
    p.add_argument('-V', '--verbose', action='store_true',
                   help='Show engine output')
    args = p.parse_args()

    if not args.text and not args.file:
        p.error('provide text as argument or use -f FILE')

    result = speak(
        text=args.text,
        text_file=args.file,
        output=args.output,
        voice=args.voice,
        pitch=args.pitch,
        verbose=args.verbose,
    )

    if result:
        size = os.path.getsize(result)
        print('%s (%s bytes)' % (result, f'{size:,}'))
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
