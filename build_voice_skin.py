"""
build_voice_skin.py -- Voice Skin Builder

Takes Tom's VIN/VDB and replaces ONLY the audio data with new recordings.
Keeps Tom's exact unit table, hash, prsl, ccos, trees, and all lp/dl values.

Usage:
    python build_voice_skin.py mara --wav-dir tom_all_rvc
    python build_voice_skin.py craig --wav-dir craig_rvc

The new recordings must have the same names as Tom's VDB recordings.
Each recording is downsampled to 8kHz, u-law encoded, and truncated/padded
to match Tom's exact byte count so all lp/dl values remain valid.
"""
import audioop
import glob
import os
import struct
import sys
import wave
import numpy as np
from scipy.signal import resample_poly
from math import gcd

XOR_KEY = 0xCE
_HERE = os.path.dirname(os.path.abspath(__file__))


def xor_codec(data):
    """XOR encode/decode (symmetric)."""
    out = bytearray(data)
    for i in range(len(out)):
        out[i] ^= XOR_KEY
    return bytes(out)


def riff_chunks(data, start=12, end=None):
    end = end or len(data)
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4]
        sz = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def load_wav_8khz_ulaw(wav_path):
    """Load a WAV, downsample to 8kHz mono, u-law encode."""
    with wave.open(wav_path, 'rb') as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    # Convert to numpy float
    if sw == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    elif sw == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 65536.0
    else:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)

    # Mono
    if nch > 1:
        samples = samples.reshape(-1, nch).mean(axis=1)

    # Resample to 8000 Hz
    if sr != 8000:
        g = gcd(sr, 8000)
        up = 8000 // g
        down = sr // g
        samples = resample_poly(samples, up, down)

    # Normalize RMS to ~6500 (Tom's level)
    rms = np.sqrt(np.mean(samples ** 2))
    if rms > 0:
        samples = samples * (6500.0 / rms)

    # Clip to int16 range
    samples = np.clip(samples, -32768, 32767).astype(np.int16)

    # u-law encode
    pcm_bytes = samples.tobytes()
    ulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)

    return ulaw_bytes


def main():
    if len(sys.argv) < 2:
        print("Usage: python build_voice_skin.py <voice_name> --wav-dir <dir>")
        sys.exit(1)

    voice_name = sys.argv[1]
    wav_dir_name = "tom_all_rvc"
    for i, arg in enumerate(sys.argv):
        if arg == '--wav-dir' and i + 1 < len(sys.argv):
            wav_dir_name = sys.argv[i + 1]

    voice_dir = os.path.join(_HERE, 'en-US', voice_name)
    tom_vin_path = os.path.join(_HERE, 'en-US', 'tom', 'tom.vin')
    tom_vdb_path = os.path.join(_HERE, 'en-US', 'tom', 'tom8.vdb')
    wav_dir = os.path.join(voice_dir, 'output', wav_dir_name)
    vin_out = os.path.join(voice_dir, '%s.vin' % voice_name)
    vdb_out = os.path.join(voice_dir, '%s8.vdb' % voice_name)

    print("=" * 70)
    print("  Voice Skin Builder")
    print("  Voice:    %s" % voice_name)
    print("  Tom VIN:  %s" % tom_vin_path)
    print("  Tom VDB:  %s" % tom_vdb_path)
    print("  WAV dir:  %s" % wav_dir)
    print("  VIN out:  %s" % vin_out)
    print("  VDB out:  %s" % vdb_out)
    print("=" * 70)

    # Kill Speechify if running
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'pid']):
            if proc.info['name'] and 'speechify' in proc.info['name'].lower():
                proc.kill()
                proc.wait(timeout=5)
                print("Killed Speechify process (pid %d)" % proc.info['pid'])
    except Exception:
        pass

    os.makedirs(voice_dir, exist_ok=True)

    # -- Load Tom's VDB --
    print("\nLoading Tom VDB...")
    tom_vdb_enc = open(tom_vdb_path, 'rb').read()
    tom_vdb = bytearray(tom_vdb_enc)
    for i in range(len(tom_vdb)):
        tom_vdb[i] ^= XOR_KEY
    tom_vdb = bytes(tom_vdb)

    assert tom_vdb[:4] == b'RIFF' and tom_vdb[8:12] == b'WAVE'

    # Find data and indx chunks
    data_ds = None
    data_sz = None
    indx_ds = None
    indx_sz = None
    fmt_ds = None
    fmt_sz = None
    for tag, ds, sz in riff_chunks(tom_vdb):
        if tag == b'data':
            data_ds = ds
            data_sz = sz
        elif tag == b'indx':
            indx_ds = ds
            indx_sz = sz
        elif tag == b'fmt ':
            fmt_ds = ds
            fmt_sz = sz

    # Parse indx
    count = struct.unpack_from('<I', tom_vdb, indx_ds)[0]
    pos = indx_ds + 4
    indx_entries = []
    for _ in range(count + 1):
        off = struct.unpack_from('<I', tom_vdb, pos)[0]
        nlen = struct.unpack_from('<H', tom_vdb, pos + 4)[0]
        name = tom_vdb[pos+6:pos+6+nlen].decode('latin-1', errors='replace').rstrip('\x00')
        indx_entries.append((off, name))
        pos += 6 + nlen

    # Build recording map: name -> (offset_in_data, size)
    tom_recs = {}
    for i in range(len(indx_entries) - 1):
        off, name = indx_entries[i]
        next_off = indx_entries[i + 1][0]
        sz = next_off - off
        tom_recs[name] = (off, sz)

    print("  %d recordings in Tom VDB" % len(tom_recs))

    # -- Load new WAVs --
    print("Loading new WAVs from %s ..." % wav_dir)
    wav_files = {}
    for f in glob.glob(os.path.join(wav_dir, "*.wav")):
        name = os.path.splitext(os.path.basename(f))[0]
        wav_files[name] = f
    print("  %d WAVs found" % len(wav_files))

    # -- Build new VDB data --
    print("Building new VDB...")
    # Start with a copy of Tom's data chunk
    data_buf = bytearray(tom_vdb[data_ds:data_ds + data_sz])

    replaced = 0
    kept_tom = 0
    padded = 0
    truncated = 0

    for name, (off, sz) in sorted(tom_recs.items()):
        if sz == 0:
            continue

        if name in wav_files:
            # Load and convert new audio
            try:
                new_ulaw = load_wav_8khz_ulaw(wav_files[name])
            except Exception as e:
                print("  WARNING: failed to load %s: %s" % (name, e))
                kept_tom += 1
                continue

            if len(new_ulaw) >= sz:
                # Truncate to Tom's exact length
                data_buf[off:off + sz] = new_ulaw[:sz]
                if len(new_ulaw) > sz:
                    truncated += 1
            else:
                # Pad with silence (u-law silence = 0xFF)
                data_buf[off:off + len(new_ulaw)] = new_ulaw
                data_buf[off + len(new_ulaw):off + sz] = b'\xff' * (sz - len(new_ulaw))
                padded += 1
            replaced += 1
        else:
            kept_tom += 1

    print("  Replaced: %d recordings" % replaced)
    print("  Kept Tom: %d recordings" % kept_tom)
    print("  Truncated: %d (new audio longer than Tom)" % truncated)
    print("  Padded: %d (new audio shorter than Tom)" % padded)

    # -- Write new VDB --
    # Reconstruct the full VDB with modified data chunk
    print("Writing %s ..." % vdb_out)
    vdb_new = bytearray(tom_vdb)
    vdb_new[data_ds:data_ds + data_sz] = data_buf

    # XOR encode
    for i in range(len(vdb_new)):
        vdb_new[i] ^= XOR_KEY

    with open(vdb_out, 'wb') as f:
        f.write(vdb_new)
    print("  Wrote %s (%d bytes)" % (vdb_out, len(vdb_new)))

    # -- Copy and patch VIN --
    print("\nBuilding VIN...")
    tom_vin_enc = open(tom_vin_path, 'rb').read()
    tom_vin = bytearray(tom_vin_enc)
    for i in range(len(tom_vin)):
        tom_vin[i] ^= XOR_KEY

    # Patch the voice name in the INFO chunk (ICOP field) if present
    # Also patch feat chunk's filename references -- but those use stored_ids
    # which are the same regardless of voice name. The engine loads by
    # directory path, not by embedded name. So we just need the file in the
    # right directory.

    # XOR encode and write
    vin_out_data = bytearray(tom_vin)
    for i in range(len(vin_out_data)):
        vin_out_data[i] ^= XOR_KEY

    with open(vin_out, 'wb') as f:
        f.write(vin_out_data)
    print("  Wrote %s (%d bytes)" % (vin_out, len(vin_out_data)))

    print("\n" + "=" * 70)
    print("  VOICE SKIN COMPLETE")
    print("  Voice: %s" % voice_name)
    print("  VIN:   %s (Tom's structure, byte-for-byte)" % vin_out)
    print("  VDB:   %s (%d/%d recordings replaced)" % (vdb_out, replaced, len(tom_recs)))
    print("=" * 70)
    print("\nRestart Speechify.exe and test.")


if __name__ == '__main__':
    main()
