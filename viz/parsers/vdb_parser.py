"""
Consolidated VDB file parser for the Speechify visualizer.
Handles VDB index, audio extraction, u-law codec, and WAV generation.
"""
import struct
import os
import io
import wave

XOR_KEY = 0xCE

# Module-level cache
_cache = {}

# u-law decode table (256 entries: u-law byte -> PCM16 sample)
ULAW_DECODE = [
    -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
    -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
    -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
    -11900,-11388,-10876,-10364, -9852, -9340, -8828, -8316,
     -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
     -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
     -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
     -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
     -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
     -1372, -1308, -1244, -1180, -1116, -1052,  -988,  -924,
      -876,  -844,  -812,  -780,  -748,  -716,  -684,  -652,
      -620,  -588,  -556,  -524,  -492,  -460,  -428,  -396,
      -372,  -356,  -340,  -324,  -308,  -292,  -276,  -260,
      -244,  -228,  -212,  -196,  -180,  -164,  -148,  -132,
      -120,  -112,  -104,   -96,   -88,   -80,   -72,   -64,
       -56,   -48,   -40,   -32,   -24,   -16,    -8,     0,
     32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
     23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
     15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
     11900, 11388, 10876, 10364,  9852,  9340,  8828,  8316,
      7932,  7676,  7420,  7164,  6908,  6652,  6396,  6140,
      5884,  5628,  5372,  5116,  4860,  4604,  4348,  4092,
      3900,  3772,  3644,  3516,  3388,  3260,  3132,  3004,
      2876,  2748,  2620,  2492,  2364,  2236,  2108,  1980,
      1884,  1820,  1756,  1692,  1628,  1564,  1500,  1436,
      1372,  1308,  1244,  1180,  1116,  1052,   988,   924,
       876,   844,   812,   780,   748,   716,   684,   652,
       620,   588,   556,   524,   492,   460,   428,   396,
       372,   356,   340,   324,   308,   292,   276,   260,
       244,   228,   212,   196,   180,   164,   148,   132,
       120,   112,   104,    96,    88,    80,    72,    64,
        56,    48,    40,    32,    24,    16,     8,     0,
]


def xor_decode(data):
    return bytes(b ^ XOR_KEY for b in data)


def riff_chunks(data, start=12, end=None):
    end = end or len(data)
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4].decode("ascii", errors="replace")
        sz = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def load_vdb(path):
    """Load and XOR-decode a VDB file. Cached."""
    path = os.path.abspath(path)
    if path not in _cache:
        with open(path, "rb") as f:
            raw = f.read()
        plain = xor_decode(raw)
        assert plain[:4] == b"RIFF", "Bad RIFF header"
        _cache[path] = plain
    return _cache[path]


def parse_vdb(plain):
    """Parse VDB structure: locate indx and data chunks, parse recording index.
    Format: indx has u32 count, then (count+1) entries of [u32 offset, u16 name_len, char[] name].
    The last entry is the end-of-data marker. Offsets are relative to start of data chunk.
    Returns dict with recordings list, data_offset, data_size."""
    chunks = {}
    for tag, off, sz in riff_chunks(plain):
        chunks[tag] = (off, sz)

    data_off = chunks.get("data", (0, 0))[0]
    data_sz = chunks.get("data", (0, 0))[1]

    recordings = []
    if "indx" in chunks:
        indx_off, indx_sz = chunks["indx"]

        count = struct.unpack_from('<I', plain, indx_off)[0]
        pos = indx_off + 4

        # Read count+1 entries (last is end-of-data sentinel)
        entries = []
        for _ in range(count + 1):
            if pos + 6 > len(plain):
                break
            offset = struct.unpack_from('<I', plain, pos)[0]
            name_len = struct.unpack_from('<H', plain, pos+4)[0]
            name = plain[pos+6:pos+6+name_len].decode('latin-1', errors='replace').rstrip('\x00')
            entries.append((offset, name))
            pos += 6 + name_len

        # Build recording list: size = next_offset - this_offset
        for i in range(len(entries) - 1):
            off_rel, name = entries[i]
            next_off_rel = entries[i+1][0]
            sz = next_off_rel - off_rel
            if sz > 0 and name:
                recordings.append({
                    "name": name,
                    "offset": off_rel,  # relative to data chunk
                    "size": sz,
                    "index": i,
                    "duration_ms": int(sz * 1000 / 8000),
                })

    return {
        "recordings": recordings,
        "data_offset": data_off,
        "data_size": data_sz,
        "n_recordings": len(recordings),
    }


def ulaw_to_pcm16(ulaw_bytes):
    """Decode u-law bytes to PCM16 sample list."""
    return [ULAW_DECODE[b] for b in ulaw_bytes]


def get_recording_pcm(vdb_plain, vdb_info, rec_name):
    """Get PCM16 samples for a recording by name."""
    for rec in vdb_info["recordings"]:
        if rec["name"] == rec_name:
            data_off = vdb_info["data_offset"]
            start = data_off + rec["offset"]
            end = start + rec["size"]
            ulaw_data = vdb_plain[start:end]
            return ulaw_to_pcm16(ulaw_data)
    return None


def get_recording_pcm_by_idx(vdb_plain, vdb_info, rec_idx):
    """Get PCM16 samples for a recording by index."""
    if rec_idx >= len(vdb_info["recordings"]):
        return None
    rec = vdb_info["recordings"][rec_idx]
    data_off = vdb_info["data_offset"]
    start = data_off + rec["offset"]
    end = start + rec["size"]
    ulaw_data = vdb_plain[start:end]
    return ulaw_to_pcm16(ulaw_data)


def pcm_to_wav_bytes(pcm_samples, sample_rate=8000):
    """Convert PCM16 sample list to WAV file bytes."""
    buf = io.BytesIO()
    wf = wave.open(buf, 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(sample_rate)
    wf.writeframes(struct.pack(f'<{len(pcm_samples)}h', *pcm_samples))
    wf.close()
    return buf.getvalue()


def get_recording_wav(vdb_plain, vdb_info, rec_name):
    """Get a full WAV file (bytes) for a recording."""
    pcm = get_recording_pcm(vdb_plain, vdb_info, rec_name)
    if pcm is None:
        return None
    return pcm_to_wav_bytes(pcm)


def get_unit_wav(vdb_plain, vdb_info, unit_dict):
    """Get WAV bytes for a single unit's audio segment.
    unit_dict must have file_idx, local_pos, dur_like fields."""
    fidx = unit_dict["file_idx"]
    lp = unit_dict["local_pos"]
    dl = unit_dict["dur_like"]

    if fidx >= len(vdb_info["recordings"]):
        return None

    rec = vdb_info["recordings"][fidx]
    data_off = vdb_info["data_offset"]
    rec_start = data_off + rec["offset"]

    # byte_offset = lp * 8 (1 unit = 1 ms, 8 samples/ms at 8kHz)
    unit_byte_start = lp * 8
    unit_byte_len = max(dl * 8, 80)  # minimum 10ms

    start = rec_start + unit_byte_start
    end = min(start + unit_byte_len, rec_start + rec["size"])

    if start >= len(vdb_plain) or end <= start:
        return None

    ulaw_data = vdb_plain[start:end]
    pcm = ulaw_to_pcm16(ulaw_data)
    return pcm_to_wav_bytes(pcm)


def get_waveform_data(vdb_plain, vdb_info, rec_name, max_points=2000):
    """Get downsampled waveform data for canvas rendering."""
    pcm = get_recording_pcm(vdb_plain, vdb_info, rec_name)
    if pcm is None:
        return None

    n = len(pcm)
    if n <= max_points:
        return {"samples": pcm, "sample_rate": 8000, "total_samples": n}

    # Downsample by taking min/max per bucket for waveform display
    bucket_size = n // (max_points // 2)
    downsampled = []
    for i in range(0, n, bucket_size):
        chunk = pcm[i:i+bucket_size]
        if chunk:
            downsampled.append(min(chunk))
            downsampled.append(max(chunk))

    return {
        "samples": downsampled,
        "sample_rate": 8000,
        "total_samples": n,
        "downsampled": True,
        "bucket_size": bucket_size,
    }
