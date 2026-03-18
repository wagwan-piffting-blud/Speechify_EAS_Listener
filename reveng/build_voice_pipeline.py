#!/usr/bin/env python3
"""Consolidated voice build pipeline. ONLY run this script if you're doing a "from scratch" voice -- all other voices using RVC, use build_voice_skin.py to avoid unnecessary reprocessing that might mess up how the voice is processed.

Combines all build steps into a single monolith script, parameterized by voice name.
Runs all 5 build steps in order:
  1. build_voice     -- builds <voice>8.vdb + <voice>.vin from WAVs + MFA alignment
  2. build_extras    -- replaces low-quality units with extra recording data
  3. build_rest      -- patches hash, prsl, and mean chunks
  4. build_hash      -- patches join-cost values in hash chunk
  5. build_trees     -- patches CART tree leaves

Usage:
  python build_voice_pipeline.py mara
  python build_voice_pipeline.py craig
  python build_voice_pipeline.py mara --skip-extras
  python build_voice_pipeline.py mara --wav-dir my_wavs --tg-dir my_tg
  python build_voice_pipeline.py mara --workers 8

Directory structure assumed:
  en-US/<voice>/                    # voice output dir
  en-US/<voice>/output/             # working files
  en-US/<voice>/output/<wav_dir>/   # source WAVs
  en-US/<voice>/output/<tg_dir>/    # MFA TextGrids
  en-US/<voice>/output/extra_wavs/  # optional extra recordings
  en-US/<voice>/output/extra_tg/    # optional extra TextGrids
  en-US/tom/tom.vin                 # template voice (always)
  en-US/tom/tom8.vdb                # template VDB (always)
"""

import argparse
import audioop
import glob as _glob
import io
import math
import os
import pickle
import random
import shutil
import struct
import sys
import tempfile
import time
import wave
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import psutil
from tqdm import tqdm


# =========================================================================== #
#  Constants                                                                  #
# =========================================================================== #
# State version -- bump whenever the algorithm or record layout changes.
# Shared by step 1 (voice) and step 2 (extras) so both caches invalidate together.
STATE_VERSION = 1

XOR_KEY   = 0xCE
UNIT_SIZE = 29
TOM_N_UNITS = 169579

# ARPAbet phone inventory (indices 0..45) matching ccos/labl and unit.phone_center.
PHONE_LABELS = [
    'aa', 'ae', 'ah', 'ao', 'aw', 'ax', 'ay', 'b', 'ch', 'dx',
    'd',  'dh', 'eh', 'el', 'er', 'en', 'ey', 'f', 'g',  'hh',
    'ih', 'ix', 'iy', 'jh', 'k',  'l',  'm',  'n', 'ng', 'ow',
    'oy', 'p',  'pau','r',  's',  'sh', 't',  'th','uh', 'uw',
    'v',  'w',  'xx', 'y',  'z',  'zh',
]
PHONE_IDX = {ph: i for i, ph in enumerate(PHONE_LABELS)}

# Phone labels for durt trees (47 entries, index 0..46)
PHONE_LABELS_47 = [
    'aa','ae','ah','ao','aw','ax','ay','b','ch','dx',
    'd','dh','eh','el','er','en','ey','f','g','hh',
    'ih','ix','iy','jh','k','l','m','n','ng','ow',
    'oy','p','pau','r','s','sh','t','th','uh','uw',
    'v','w','xx','y','z','zh','',
]

_SILENCE_LABELS = {'', 'sil', 'sp', 'spn', 'silence', '<sil>', '<unk>'}
_SILENCE_PCS = {32, 255}
HP_SILENCE   = 92

# HP_BASE: 2*pc with 5 confirmed anomalies
HP_BASE = [2 * pc for pc in range(46)]
HP_BASE[9]  = 22   # aw2
HP_BASE[10] = 18   # ax1
HP_BASE[11] = 20   # ax2
HP_BASE[14] = 30   # b1
HP_BASE[15] = 28   # b2

# F0 scaling
F0_SCALE   = 0.641
TOM_F0_MIN = 99
TOM_F0_MAX = 150

# Audio processing
TARGET_RMS  = 6500.0
WSOLA_PAD   = 8192   # silence padding appended to VDB data

# Spectral envelope normalization
SPEC_FRAME  = 256       # 32 ms at 8 kHz
SPEC_HOP    = 128       # 16 ms hop
SPEC_N_MELS = 40        # number of mel bands for EQ
SPEC_MAX_DB = 20.0      # max per-band correction in dB

# Step 1 unit count
STEP1_N_UNITS = 169579

# Step 1 post-processing
MIN_DL_FLOOR    = 10   # absolute minimum dl for speech units
MAX_LP_GAP      = 5    # allow small gaps, close anything larger
MIN_LP_SPACING  = 15   # minimum ms between consecutive units in same recording

# Step 2 extras
EXTRA_MIN_PHONE_DUR_MS = 5    # skip intervals shorter than this
EXTRA_MIN_UNIT_DUR     = 25   # minimum lp spacing between halfphones

# Step 3 (rest) prsl settings
MIN_BACKOFF_CANDS   = 10
MAX_CANDS_PER_GROUP = 200
MIN_RUN_POTENTIAL   = 3
# High-coverage target recordings
TARGET_RECORDING_FIDX = [2106, 4520, 4905]  # news09_035, news32_047, news7_032

# Step 4 (hash) settings
SENTINEL       = 0xFFFFFFFF
MISS_PENALTY   = 0.0
BOUNDARY_MS    = 8
BOUNDARY_SAMP  = 64
N_FFT_BINS     = 33
N_FEATURES     = 12
COST_SCALE     = 0.0
CLUSTER_DISCOUNT    = 0.3
CLUSTER_DIST_PCTILE = 25
RUN_PENALTY_MIN     = 6
RUN_PENALTY_MULT    = 8.0

# Step 5 (trees)
TREE_MIN_SAMPLES     = 5
SKIP_DURT_RECOMPUTE  = True


# =========================================================================== #
#  IPA -> ARPAbet mapping for english_mfa TextGrid output                     #
# =========================================================================== #
_IPA_TO_ARPA = {
    # --- Vowels ---
    '\u0251\u02d0': 'aa', '\u0251': 'aa',
    'a\u02d0': 'aa', 'a': 'aa',
    '\xe6': 'ae',
    '\u0250': 'ah', '\u028c': 'ah',
    '\u0259': 'ah',
    '\u0254': 'ao', '\u0252': 'ao',
    '\u0252\u02d0': 'ao',
    'aw': 'aw',
    'aj': 'ay',
    '\u025b': 'eh',
    'e\u02d0': 'ey', 'e': 'eh',
    '\u025d': 'er', '\u025a': 'er',
    '\u025c\u02d0': 'er',
    'ej': 'ey',
    '\u026a': 'ih',
    'i\u02d0': 'iy', 'i': 'iy',
    '\u028a': 'uh',
    'u\u02d0': 'uw', 'u': 'uw', '\u0289\u02d0': 'uw', '\u0289': 'uw',
    'ow': 'ow',
    '\u0259w': 'ow',
    '\u0254j': 'oy',
    # --- Consonants (plain) ---
    'b': 'b',
    't\u0283': 'ch',
    'd': 'd',
    '\xf0': 'dh',
    'f': 'f',
    '\u0261': 'g',
    'h': 'hh',
    'd\u0292': 'jh',
    'k': 'k',
    'l': 'l', '\u026b': 'l', '\u026b\u0329': 'l',
    'm': 'm', 'm\u0329': 'm',
    'n': 'n', 'n\u0329': 'n',
    '\u014b': 'ng',
    'p': 'p',
    '\u0279': 'r',
    's': 's',
    '\u0283': 'sh',
    't': 't',
    '\u03b8': 'th',
    'v': 'v',
    'w': 'w',
    'j': 'y',
    'z': 'z',
    '\u0292': 'zh',
    '\u027e': 't',
    # --- Palatalized consonants (english_mfa: X + U+02B2) ---
    't\u02b2': 't', 'd\u02b2': 'd', 'b\u02b2': 'b', 'f\u02b2': 'f',
    'm\u02b2': 'm', 'n\u02b2': 'n', 'k\u02b2': 'k', 'p\u02b2': 'p',
    'v\u02b2': 'v', 'l\u02b2': 'l', 's\u02b2': 's', 'z\u02b2': 'z',
    '\u0261\u02b2': 'g', 'h\u02b2': 'hh', '\u0279\u02b2': 'r',
    # --- Dental variants (X + U+032A) ---
    't\u032a': 'th', 'd\u032a': 'dh',
    # --- Retroflex (english_mfa) ---
    '\u0288': 't', '\u0256': 'd',
    # --- Palatal (english_mfa) ---
    '\u028e': 'l', '\u0272': 'n', '\u00e7': 'sh',
    'c': 'k', '\u025f': 'jh',
    # --- Labio-dental approximant ---
    '\u028b': 'v',
}

# Map Tom's allophonic distinctions to MFA's broader phone set.
_PHONE_NORM = {
    'ax': 'ah',
    'ix': 'ih',
    'dx': 't',
    'el': 'l',
    'en': 'n',
}

# MFA labels that are NOT real phones (step 1)
_REAL_PHONE_SET = {'', 'spn', 'sil', 'sp'}


# =========================================================================== #
#  Shared Utilities                                                           #
# =========================================================================== #
def kill_speechify():
    """Kill any running Speechify process to free file locks."""
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.info['name'] and proc.info['name'].lower() in ("speechify.exe", "speechify"):
            proc.kill()
            proc.wait(timeout=5)
            print("Killed Speechify process (pid %d) to free file locks." % proc.info['pid'])


def xor_decode(path):
    """Read a file and XOR-decode it with XOR_KEY."""
    raw = np.fromfile(path, dtype=np.uint8)
    raw ^= XOR_KEY
    return raw.tobytes()


def xor_encode(data):
    """XOR-encode data with XOR_KEY."""
    arr = np.frombuffer(data, dtype=np.uint8).copy()
    arr ^= XOR_KEY
    return arr.tobytes()


def xor_codec(data):
    """Simple byte-by-byte XOR codec (used in step 1)."""
    return bytes(b ^ XOR_KEY for b in data)


def load_encoded(path):
    """Load and XOR-decode a file (step 1 style)."""
    with open(path, 'rb') as f:
        return xor_codec(f.read())


def riff_chunks(data, start=0, end=None):
    """Yield (tag, data_start, size) for RIFF chunks."""
    if end is None:
        end = len(data)
    pos = start
    while pos + 8 <= end:
        tag = bytes(data[pos:pos+4]) if isinstance(data, (bytearray, memoryview)) else data[pos:pos+4]
        sz  = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def riff_chunks_12(data, start=12, end=None):
    """Yield (tag, data_start, size) for RIFF chunks starting at offset 12 (step 1 style)."""
    end = end or len(data)
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4]
        sz  = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def pack_chunk(tag, body):
    """Pack a RIFF chunk: tag + u32 size + body + optional pad byte."""
    if isinstance(tag, str):
        tag = tag.encode('latin1')
    pad = b'\x00' if len(body) & 1 else b''
    return tag + struct.pack('<I', len(body)) + body + pad


def _file_key(path):
    """Return (mtime, size) for a file, or None if it does not exist."""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def normalize_phone(label):
    """Normalize a phone label from IPA/MFA to ARPAbet."""
    lbl = label.strip().lower().rstrip('012')
    if lbl in _SILENCE_LABELS:
        return 'pau'
    arpa = _IPA_TO_ARPA.get(lbl)
    if arpa:
        return arpa
    if lbl.endswith('\u02d0'):
        arpa = _IPA_TO_ARPA.get(lbl[:-1])
        if arpa:
            return arpa
    if lbl.endswith('\u02b2'):
        arpa = _IPA_TO_ARPA.get(lbl[:-1])
        if arpa:
            return arpa
    if len(lbl) > 3 or (len(lbl) > 1 and lbl.isascii() and lbl.isalpha()):
        return 'pau'
    return lbl


def _norm_phone(ph):
    """Normalize a phone label for cross-inventory comparison."""
    return _PHONE_NORM.get(ph, ph)


def downsample_3x(pcm_bytes):
    """Downsample 24kHz PCM16 to 8kHz using polyphase resampling."""
    from scipy.signal import resample_poly
    samples = np.frombuffer(pcm_bytes, dtype='<i2').copy().astype(np.float64)
    out = resample_poly(samples, 1, 3)
    return np.clip(out, -32768, 32767).astype('<i2').tobytes()


def normalize_rms(pcm_bytes, target_rms=TARGET_RMS):
    """Scale PCM16 bytes so the recording RMS equals target_rms."""
    samples = np.frombuffer(pcm_bytes, dtype='<i2').astype(np.float64)
    rms = np.sqrt(np.mean(samples ** 2)) if len(samples) > 0 else 0.0
    if rms < 200.0:
        return pcm_bytes
    scale = target_rms / rms
    out = np.clip(samples * scale, -32768, 32767).astype('<i2')
    return out.tobytes()


def pcm16_to_ulaw(pcm_bytes):
    """Convert PCM16 to G.711 u-law."""
    return audioop.lin2ulaw(pcm_bytes, 2)


def f0_track_from_ulaw(ulaw_bytes):
    """Return PyWorld HARVEST F0 array (1 value/ms) or None on failure."""
    import pyworld as pw
    if len(ulaw_bytes) < 800:
        return None
    try:
        pcm = audioop.ulaw2lin(ulaw_bytes, 2)
        x = np.frombuffer(pcm, dtype='<i2').astype(np.float64) / 32768.0
        f0, _ = pw.harvest(x, 8000, frame_period=1.0)
        if np.all(f0 == 0.0):
            return None
        return f0
    except Exception:
        return None


def lookup_f0(f0_arr, pos_ms):
    """Look up F0 value at a given ms position, scaled to Tom's range."""
    if f0_arr is None or len(f0_arr) == 0:
        return 0
    idx = max(0, min(len(f0_arr) - 1, int(pos_ms)))
    hz = f0_arr[idx]
    if hz < 50.0:
        return 0
    scaled = round(hz * F0_SCALE)
    if scaled < TOM_F0_MIN:
        return 0
    return min(TOM_F0_MAX, scaled)


def unit_hp(pc, is_first_half):
    """Compute halfphone index from phone center and is_first_half."""
    if pc in _SILENCE_PCS or pc >= 46:
        return HP_SILENCE
    return HP_BASE[pc] + (1 - int(is_first_half))


def compute_context_key(left_hp, pc, is_first_half, right_hp):
    """Compute PRSL context key from halfphone indices."""
    if pc in _SILENCE_PCS or pc >= 46:
        return None
    base = HP_BASE[pc]
    if left_hp == 0:
        center_hp = base
    else:
        center_hp = base + (1 if left_hp == HP_SILENCE and right_hp != HP_SILENCE
                            else left_hp % 2)
    return left_hp * 10000 + center_hp * 100 + right_hp


# -- Spectral envelope normalization helpers --
_SPEC_WIN = None
_SPEC_FB  = None


def _init_spec_globals():
    """Lazy-initialize spectral normalization globals."""
    global _SPEC_WIN, _SPEC_FB
    if _SPEC_WIN is not None:
        return
    _SPEC_WIN = np.hanning(SPEC_FRAME).astype(np.float64)
    _SPEC_FB = _build_mel_filterbank(SPEC_FRAME, 8000, SPEC_N_MELS)


def _build_mel_filterbank(n_fft, sr, n_mels):
    """Build a mel-scale filterbank matrix (n_mels x n_fft//2+1)."""
    def hz2mel(h):
        return 2595.0 * np.log10(1.0 + h / 700.0)
    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    n_bins = n_fft // 2 + 1
    lo, hi = hz2mel(0.0), hz2mel(sr / 2.0)
    mel_pts = np.linspace(lo, hi, n_mels + 2)
    hz_pts = mel2hz(mel_pts)
    bins = np.floor((n_fft + 1) * hz_pts / sr).astype(int)
    fb = np.zeros((n_mels, n_bins), dtype=np.float64)
    for m in range(1, n_mels + 1):
        lo_b, mid_b, hi_b = int(bins[m - 1]), int(bins[m]), int(bins[m + 1])
        if mid_b > lo_b:
            fb[m - 1, lo_b:mid_b] = np.linspace(0, 1, mid_b - lo_b, endpoint=False)
        if hi_b > mid_b:
            fb[m - 1, mid_b:hi_b] = np.linspace(1, 0, hi_b - mid_b, endpoint=False)
    return fb


def compute_avg_mel_spectrum(pcm_bytes):
    """Compute average mel-band power spectrum for a PCM16 recording."""
    _init_spec_globals()
    samples = np.frombuffer(pcm_bytes, dtype='<i2').astype(np.float64)
    n = len(samples)
    if n < SPEC_FRAME:
        return None
    n_frames = (n - SPEC_FRAME) // SPEC_HOP + 1
    if n_frames < 1:
        return None
    avg_mel = np.zeros(SPEC_N_MELS, dtype=np.float64)
    for i in range(n_frames):
        frame = samples[i * SPEC_HOP : i * SPEC_HOP + SPEC_FRAME]
        windowed = frame * _SPEC_WIN
        power = np.abs(np.fft.rfft(windowed)) ** 2
        mel = _SPEC_FB @ power
        avg_mel += mel
    avg_mel /= n_frames
    return avg_mel


def spectral_eq(pcm_bytes, rec_mel, target_mel, max_db=SPEC_MAX_DB):
    """Apply mel-band EQ to match target spectral envelope."""
    _init_spec_globals()
    samples = np.frombuffer(pcm_bytes, dtype='<i2').astype(np.float64)
    n = len(samples)
    if n < SPEC_FRAME:
        return pcm_bytes

    eps = 1e-10
    gains_mel = np.sqrt((target_mel + eps) / (rec_mel + eps))
    max_lin = 10.0 ** (max_db / 20.0)
    min_lin = 10.0 ** (-max_db / 20.0)
    gains_mel = np.clip(gains_mel, min_lin, max_lin)

    n_bins = SPEC_FRAME // 2 + 1
    fb_sum = _SPEC_FB.sum(axis=0) + eps
    gains_freq = (_SPEC_FB.T @ gains_mel) / fb_sum
    outside = fb_sum < eps * 2
    gains_freq[outside] = 1.0

    output = np.zeros(n, dtype=np.float64)
    weight = np.zeros(n, dtype=np.float64)
    n_frames = (n - SPEC_FRAME) // SPEC_HOP + 1
    for i in range(n_frames):
        start = i * SPEC_HOP
        end = start + SPEC_FRAME
        frame = samples[start:end] * _SPEC_WIN
        spec = np.fft.rfft(frame)
        spec *= gains_freq
        frame_out = np.fft.irfft(spec, n=SPEC_FRAME)
        output[start:end] += frame_out * _SPEC_WIN
        weight[start:end] += _SPEC_WIN ** 2
    weight[weight < eps] = 1.0
    output /= weight
    last_end = (n_frames - 1) * SPEC_HOP + SPEC_FRAME if n_frames > 0 else 0
    if last_end < n:
        output[last_end:] = samples[last_end:]
    return np.clip(output, -32768, 32767).astype('<i2').tobytes()


# -- TextGrid parsers --
def parse_textgrid_all(path):
    """Return (all_intervals, xmax) for the phones tier.
    all_intervals: [(start_sec, end_sec, label)] including silence.
    """
    all_intervals = []
    xmax = 0.0
    xmax_found = False
    in_phones_tier = False
    in_interval = False
    cur_xmin = cur_xmax_iv = None

    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not xmax_found and line.startswith('xmax ='):
                try:
                    xmax = float(line.split('=', 1)[1].strip())
                    xmax_found = True
                except ValueError:
                    pass
            if 'name = "phones"' in line:
                in_phones_tier = True
            elif in_phones_tier:
                if line.startswith('intervals ['):
                    in_interval = True
                    cur_xmin = cur_xmax_iv = None
                elif in_interval:
                    if line.startswith('xmin ='):
                        try:
                            cur_xmin = float(line.split('=', 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith('xmax ='):
                        try:
                            cur_xmax_iv = float(line.split('=', 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith('text ='):
                        label = line.split('=', 1)[1].strip().strip('"')
                        if cur_xmin is not None and cur_xmax_iv is not None:
                            all_intervals.append((cur_xmin, cur_xmax_iv, label))
                        in_interval = False
                elif line.startswith('item ['):
                    break

    return all_intervals, xmax


def parse_textgrid_phones(path):
    """Return (intervals, xmax) for the phones tier (extras style).
    intervals: [(start_sec, end_sec, arpa_label)]
    """
    intervals = []
    xmax = 0.0
    xmax_found = False
    in_phones = False
    in_iv = False
    cur_xmin = cur_xmax = None

    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not xmax_found and line.startswith('xmax ='):
                try:
                    xmax = float(line.split('=', 1)[1].strip())
                    xmax_found = True
                except ValueError:
                    pass
            elif '"phones"' in line.lower() or '"phone"' in line.lower():
                in_phones = True
            elif in_phones:
                if line.startswith('intervals ['):
                    in_iv = True
                    cur_xmin = cur_xmax = None
                elif in_iv:
                    if line.startswith('xmin ='):
                        try:
                            cur_xmin = float(line.split('=', 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith('xmax ='):
                        try:
                            cur_xmax = float(line.split('=', 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith('text ='):
                        label = line.split('=', 1)[1].strip().strip('"')
                        if cur_xmin is not None and cur_xmax is not None:
                            arpa = normalize_phone(label)
                            intervals.append((cur_xmin, cur_xmax, arpa))
                        in_iv = False
                elif line.startswith('item ['):
                    break

    return intervals, xmax


# =========================================================================== #
#  Step 1 helpers (from build_mara_voice.py)                                  #
# =========================================================================== #
def interval_at_time(all_intervals, t_sec):
    """Binary-search all_intervals; return (start, end, label) containing t_sec."""
    lo, hi = 0, len(all_intervals) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e, lbl = all_intervals[mid]
        if t_sec < s:
            hi = mid - 1
        elif t_sec >= e:
            lo = mid + 1
        else:
            return s, e, lbl
    if all_intervals:
        if t_sec <= all_intervals[0][0]:
            return all_intervals[0]
        return all_intervals[-1]
    return 0.0, 0.0, ''


def phone_at_time(all_intervals, t_sec):
    s, e, lbl = interval_at_time(all_intervals, t_sec)
    return normalize_phone(lbl)


def _build_phone_groups(units):
    """Split lp-sorted units into consecutive runs of the same phone_center.
    Returns (speech_groups, silence_units).
    """
    sorted_units = sorted(units, key=lambda u: u[1])
    runs = []
    silence_units = []
    for unit in sorted_units:
        uid, lp, dl, is_first, pc = unit
        if pc in _SILENCE_PCS:
            silence_units.append(unit)
            continue
        if runs and runs[-1][0] == pc:
            runs[-1][1].append(unit)
        else:
            runs.append((pc, [unit]))

    speech_groups = []
    for pc, grp in runs:
        lp_min    = grp[0][1]
        lp_end    = max(u[1] + u[2] for u in grp)
        tom_span  = max(1, lp_end - lp_min)
        speech_groups.append((lp_min, tom_span, grp))
    return speech_groups, silence_units


def _seq_align(speech_groups, mfa_speech, min_match_frac=0.15):
    """Align Tom speech groups to MFA intervals via Needleman-Wunsch sequence alignment."""
    n = len(speech_groups)
    m = len(mfa_speech)
    if n == 0 or m == 0:
        return None

    tom_phs = [_norm_phone(PHONE_LABELS[g[2][0][4]])
               if g[2][0][4] < len(PHONE_LABELS) else ''
               for g in speech_groups]
    mfa_phs = [_norm_phone(normalize_phone(iv[2])) for iv in mfa_speech]

    _VOWELS = {'aa', 'ae', 'ah', 'ao', 'aw', 'ay', 'eh', 'er', 'ey',
               'ih', 'iy', 'ow', 'oy', 'uh', 'uw'}
    def _sim(a, b):
        if a == b:
            return 2
        if a in _VOWELS and b in _VOWELS:
            return 1
        if a not in _VOWELS and b not in _VOWELS and a and b:
            return 0
        return -1

    GAP = -0.5

    dp_prev = [GAP * j for j in range(m + 1)]
    dp_cur = [0.0] * (m + 1)
    bt = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        bt[0][j] = 2

    for i in range(1, n + 1):
        dp_cur[0] = GAP * i
        bt[i][0] = 1
        for j in range(1, m + 1):
            s_diag = dp_prev[j - 1] + _sim(tom_phs[i - 1], mfa_phs[j - 1])
            s_skip_tom = dp_prev[j] + GAP
            s_skip_mfa = dp_cur[j - 1] + GAP
            if s_diag >= s_skip_tom and s_diag >= s_skip_mfa:
                dp_cur[j] = s_diag
                bt[i][j] = 0
            elif s_skip_tom >= s_skip_mfa:
                dp_cur[j] = s_skip_tom
                bt[i][j] = 1
            else:
                dp_cur[j] = s_skip_mfa
                bt[i][j] = 2
        dp_prev, dp_cur = dp_cur, dp_prev

    pairs = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and bt[i][j] == 0:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or bt[i][j] == 1):
            i -= 1
        else:
            j -= 1
    pairs.reverse()

    if len(pairs) < min_match_frac * min(n, m):
        return None
    return pairs


def _refine_mfa_interval(ulaw_bytes, start_ms, end_ms, cap):
    """Refine an MFA phone interval so it points to audible speech."""
    if ulaw_bytes is None or len(ulaw_bytes) < 160:
        return start_ms, end_ms
    RMS_THRESH = 800
    CHECK_WIN = 10
    check_nb = CHECK_WIN * 8
    phone_dur = max(1, end_ms - start_ms)

    def _rms_at(pos_ms):
        bo = pos_ms * 8
        if bo < 0 or bo + check_nb > len(ulaw_bytes):
            return 0.0
        try:
            p = audioop.ulaw2lin(ulaw_bytes[bo:bo + check_nb], 2)
            s = np.frombuffer(p, dtype='<i2').astype(np.float64)
            return np.sqrt(np.mean(s ** 2)) if len(s) >= 4 else 0.0
        except Exception:
            return 0.0

    if _rms_at(start_ms) >= RMS_THRESH:
        refined_end = end_ms
        if _rms_at(max(0, end_ms - CHECK_WIN)) < RMS_THRESH:
            for off in range(1, min(end_ms - start_ms, cap)):
                cand = end_ms - off
                if cand <= start_ms:
                    break
                if _rms_at(max(0, cand - CHECK_WIN)) >= RMS_THRESH:
                    refined_end = cand
                    break
        return start_ms, refined_end

    max_search = max(cap, start_ms)
    best_start = None
    for off in range(1, max_search):
        fwd = start_ms + off
        if fwd + check_nb // 8 <= cap and _rms_at(fwd) >= RMS_THRESH:
            best_start = fwd
            break
        bwd = start_ms - off
        if bwd >= 0 and _rms_at(bwd) >= RMS_THRESH:
            best_start = bwd
            break

    if best_start is not None:
        refined_start = max(0, best_start)
        refined_end = min(cap, refined_start + phone_dur)
        if _rms_at(refined_start) >= RMS_THRESH:
            return refined_start, refined_end

    return -1, -1


def process_recording(rec_name, units, tom_max_end, mara_n, cap, ulaw_bytes,
                      all_intervals=None, xmax=0.0):
    """Compute new (lp, dl, pc, f0s, f0e, f0m) for every unit in one recording."""
    f0_arr = None
    f0_status = 0
    if ulaw_bytes is not None:
        f0_arr = f0_track_from_ulaw(ulaw_bytes)
        f0_status = 1 if f0_arr is not None else -1

    rec_units_out = []
    if mara_n == 0:
        for uid, lp, dl, is_first, pc in units:
            rec_units_out.append((uid, lp, max(1, dl), 255, -1, -1, -1))
        return rec_units_out, f0_status, 0

    use_mfa = (all_intervals is not None and
               any(lbl not in _REAL_PHONE_SET for _, _, lbl in all_intervals))

    if use_mfa:
        mfa_speech = [(s, e, l) for s, e, l in all_intervals
                      if l not in _REAL_PHONE_SET]
        speech_groups, silence_units = _build_phone_groups(units)
        pairs = _seq_align(speech_groups, mfa_speech)

        if pairs is not None and len(pairs) >= 1:
            matched_groups = {}
            for ti, mi in pairs:
                tom_pc = speech_groups[ti][2][0][4]
                tom_ph = _norm_phone(PHONE_LABELS[tom_pc]) if tom_pc < len(PHONE_LABELS) else ''
                mfa_s, mfa_e, mfa_lbl = mfa_speech[mi]
                mfa_ph = _norm_phone(normalize_phone(mfa_lbl))
                if tom_ph != mfa_ph:
                    continue
                raw_start = max(0, min(cap, round(mfa_s * 1000)))
                raw_end = max(raw_start, min(cap, round(mfa_e * 1000)))
                ref_start, ref_end = _refine_mfa_interval(
                    ulaw_bytes, raw_start, raw_end, cap)
                if ref_start < 0:
                    continue
                matched_groups[ti] = (ref_start, ref_end)

            n_matched = 0
            n_unmatched = 0
            MIN_UNIT_DUR = 25
            for gi, (lp_min, tom_span, grp) in enumerate(speech_groups):
                if gi in matched_groups:
                    ms, me = matched_groups[gi]
                    mfa_span = max(1, me - ms)
                    n = len(grp)
                    tom_lps = [u[1] for u in grp]
                    tom_grp_span = tom_lps[-1] - tom_lps[0] if n > 1 else 0
                    if n > 1 and tom_grp_span > 0:
                        tom_unit_dur = tom_grp_span / (n - 1)
                        unit_dur = max(MIN_UNIT_DUR, round(tom_unit_dur))
                    else:
                        unit_dur = max(MIN_UNIT_DUR, mfa_span // max(1, n))
                    for idx, (uid, lp, dl, is_first, pc) in enumerate(grp):
                        new_lp = max(0, min(cap, ms + idx * unit_dur))
                        new_dl = max(unit_dur, min(cap - new_lp,
                                                   max(1, me - new_lp)))
                        f0s = lookup_f0(f0_arr, new_lp)
                        f0e = lookup_f0(f0_arr, new_lp + new_dl)
                        f0m = lookup_f0(f0_arr, new_lp + new_dl // 2)
                        rec_units_out.append((uid, new_lp, new_dl, 255,
                                              f0s, f0e, f0m))
                        n_matched += 1
                else:
                    denom = max(1, tom_max_end * 8)
                    for uid, lp, dl, is_first, pc in grp:
                        new_lp = max(0, min(cap, round(lp * mara_n / denom)))
                        new_dl = max(1, min(cap - new_lp, max(1, round(dl * mara_n / denom))))
                        f0s = lookup_f0(f0_arr, new_lp)
                        f0e = lookup_f0(f0_arr, new_lp + new_dl)
                        f0m = lookup_f0(f0_arr, new_lp + new_dl // 2)
                        rec_units_out.append((uid, new_lp, new_dl, 255,
                                              f0s, f0e, f0m))
                        n_unmatched += 1

            for uid, lp, dl, is_first, pc in silence_units:
                sil_ivs = [(s, e) for s, e, l in all_intervals
                           if l in _REAL_PHONE_SET and l != '']
                if sil_ivs:
                    tom_total = max(1, max(u[1] + u[2] for u in units))
                    rel = lp / tom_total if tom_total > 0 else 0.0
                    target = rel * (mara_n / 8000.0)
                    best = min(sil_ivs, key=lambda iv: abs((iv[0]+iv[1])/2 - target))
                    new_lp = max(0, min(cap, round(best[0] * 1000)))
                    scale = mara_n / max(1, tom_total * 8)
                    new_dl = max(1, min(cap - new_lp, max(1, round(dl * scale))))
                else:
                    new_lp = 0
                    tom_total = max(1, max(u[1] + u[2] for u in units))
                    scale = mara_n / max(1, tom_total * 8)
                    new_dl = max(1, min(cap - new_lp, max(1, round(dl * scale))))
                f0s = lookup_f0(f0_arr, new_lp)
                f0e = lookup_f0(f0_arr, new_lp + new_dl)
                f0m = lookup_f0(f0_arr, new_lp + new_dl // 2)
                rec_units_out.append((uid, new_lp, new_dl, 255,
                                      f0s, f0e, f0m))

            _uid_tom_lp = {u[0]: u[1] for u in units}
            rec_units_out.sort(key=lambda e: _uid_tom_lp.get(e[0], 0))
            _prev = -(MIN_UNIT_DUR + 1)
            _fixed = []
            for _uid, _lp, _dl, _pc, _f0s, _f0e, _f0m in rec_units_out:
                _min_lp = _prev + MIN_UNIT_DUR
                if _dl > 0 and _lp < _min_lp:
                    _lp = min(_min_lp, cap)
                    _dl = max(1, min(cap - _lp, _dl))
                if _dl > 0:
                    _prev = _lp
                _fixed.append((_uid, _lp, _dl, _pc, _f0s, _f0e, _f0m))
            rec_units_out = _fixed

            mfa_mode = 2 if len(speech_groups) != len(mfa_speech) else 1
            return rec_units_out, f0_status, mfa_mode

    # Fallback: proportional scaling (no MFA)
    if tom_max_end > 0:
        denom = tom_max_end * 8
        _MIN_FB_SPACING = 15
        _prev_fb = -(_MIN_FB_SPACING + 1)
        for uid, lp, dl, is_first, pc in units:
            new_lp = max(0, min(cap, round(lp * mara_n / denom)))
            new_dl = max(1, min(cap - new_lp, max(1, round(dl * mara_n / denom))))
            _min_fb_lp = _prev_fb + _MIN_FB_SPACING
            if new_dl > 0 and new_lp < _min_fb_lp:
                new_lp = min(_min_fb_lp, cap)
                new_dl = max(1, min(cap - new_lp, new_dl))
            if new_dl > 0:
                _prev_fb = new_lp
            f0s = lookup_f0(f0_arr, new_lp)
            f0e = lookup_f0(f0_arr, new_lp + new_dl)
            f0m = lookup_f0(f0_arr, new_lp + new_dl // 2)
            rec_units_out.append((uid, new_lp, new_dl, 255, f0s, f0e, f0m))
    else:
        for uid, lp, dl, is_first, pc in units:
            rec_units_out.append((uid, lp, dl, 255, -1, -1, -1))
    return rec_units_out, f0_status, 0


# =========================================================================== #
#  Step 2 helpers (from build_mara_extra.py)                                  #
# =========================================================================== #
def fit_f0_context(tom_vin_data):
    """Fit log(dur_like) -> f0_context from Tom's units.
    Returns a function(dur_like) -> u8 f0_context value.
    """
    for tag, ds, sz in riff_chunks(tom_vin_data, 12):
        if tag == b'unit':
            for stag, sds, ssz in riff_chunks(tom_vin_data, ds, ds + sz):
                if stag == b'data':
                    n = ssz // UNIT_SIZE
                    dls = []
                    ctxs = []
                    for i in range(n):
                        base = sds + i * UNIT_SIZE
                        dl = struct.unpack_from('<H', tom_vin_data, base + 10)[0]
                        fc = tom_vin_data[base + 19]
                        if dl > 0 and fc > 0:
                            dls.append(math.log(dl + 1))
                            ctxs.append(fc)
                    break
            break

    dls = np.array(dls)
    ctxs = np.array(ctxs, dtype=np.float64)
    A = np.column_stack([dls, np.ones_like(dls)])
    result = np.linalg.lstsq(A, ctxs, rcond=None)
    a, b = result[0]
    r2 = 1.0 - np.sum((ctxs - (a * dls + b))**2) / np.sum((ctxs - ctxs.mean())**2)
    print("  f0_context regression: f0ctx = %.3f * log(dl+1) + %.3f  (R^2=%.4f)" % (a, b, r2))

    def predict(dur_like):
        if dur_like <= 0:
            return 1
        val = round(a * math.log(dur_like + 1) + b)
        return max(1, min(255, val))

    return predict


def create_unit_fields_extra(intervals, cap, f0_arr, f0ctx_fn):
    """Create halfphone unit field tuples from MFA phoneme intervals (extras)."""
    phones = []
    for start, end, label in intervals:
        dur_ms = round((end - start) * 1000)
        if dur_ms < EXTRA_MIN_PHONE_DUR_MS:
            continue
        pc = PHONE_IDX.get(label, -1)
        if pc < 0:
            continue
        lp = round(start * 1000)
        phones.append((lp, dur_ms, pc, label))

    fields = []
    for pi, (lp, dur_ms, pc, label) in enumerate(phones):
        dl_first = max(EXTRA_MIN_UNIT_DUR, dur_ms // 2)
        dl_second = max(EXTRA_MIN_UNIT_DUR, dur_ms - dl_first)
        lp_first = lp
        lp_second = lp + dl_first
        if cap > 0:
            lp_first = min(lp_first, cap)
            dl_first = min(dl_first, max(0, cap - lp_first))
            lp_second = min(lp_second, cap)
            dl_second = min(dl_second, max(0, cap - lp_second))

        f0s_1 = lookup_f0(f0_arr, lp_first)
        f0e_1 = lookup_f0(f0_arr, lp_first + dl_first)
        f0m_1 = lookup_f0(f0_arr, lp_first + dl_first // 2)
        f0s_2 = lookup_f0(f0_arr, lp_second)
        f0e_2 = lookup_f0(f0_arr, lp_second + dl_second)
        f0m_2 = lookup_f0(f0_arr, lp_second + dl_second // 2)
        f0ctx = f0ctx_fn(dur_ms)

        fields.append((lp_first, dl_first, pc, 1, f0s_1, f0e_1, f0m_1, f0ctx))
        fields.append((lp_second, dl_second, pc, 0, f0s_2, f0e_2, f0m_2, f0ctx))

    return fields


def process_extra_recording(name, wav_path, tg_path, f0ctx_fn):
    """Process one extra recording: WAV load, downsample, u-law, F0, MFA parse."""
    with wave.open(wav_path) as w:
        n_ch = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        raw_pcm = w.readframes(w.getnframes())

    if n_ch != 1 or sampwidth != 2:
        return None

    if framerate == 24000:
        pcm_8k = downsample_3x(raw_pcm)
    elif framerate == 8000:
        pcm_8k = raw_pcm
    else:
        from scipy.signal import resample_poly
        g = math.gcd(framerate, 8000)
        up, down = 8000 // g, framerate // g
        samples = np.frombuffer(raw_pcm, dtype='<i2').copy().astype(np.float64)
        out = resample_poly(samples, up, down)
        pcm_8k = np.clip(out, -32768, 32767).astype('<i2').tobytes()

    ulaw = pcm16_to_ulaw(normalize_rms(pcm_8k))
    mara_n = len(ulaw)
    cap = mara_n // 8 - 1 if mara_n >= 16 else 0
    if cap <= 0:
        return None

    intervals, xmax = parse_textgrid_phones(tg_path)
    real_phones = [(s, e, l) for s, e, l in intervals
                   if l not in _SILENCE_LABELS and l != 'pau']
    if not real_phones and not intervals:
        return None

    f0_arr = f0_track_from_ulaw(ulaw)
    f0_status = 1 if f0_arr is not None else -1

    unit_fields = create_unit_fields_extra(intervals, cap, f0_arr, f0ctx_fn)
    if not unit_fields:
        return None

    return (ulaw, unit_fields, f0_status)


def compute_run_potential_extras(vin_data, unit_data_ds, n_units):
    """Compute run_potential for every unit (extras step)."""
    file_idxs = np.zeros(n_units, dtype=np.uint16)
    dls = np.zeros(n_units, dtype=np.uint16)

    for i in range(n_units):
        base = unit_data_ds + i * UNIT_SIZE
        file_idxs[i] = struct.unpack_from('<H', vin_data, base + 4)[0]
        dls[i] = struct.unpack_from('<H', vin_data, base + 10)[0]

    file_groups = defaultdict(list)
    for uid in range(n_units):
        if dls[uid] > 0:
            file_groups[file_idxs[uid]].append(uid)

    run_potential = np.full(n_units, -1, dtype=np.int32)
    for fid, uids in file_groups.items():
        group_size = len(uids)
        for uid in uids:
            run_potential[uid] = group_size

    return run_potential


# =========================================================================== #
#  Step 5 helpers (from build_mara_trees.py)                                  #
# =========================================================================== #
def parse_tree_with_offsets(data):
    """Parse tree nodes and record byte offsets of leaf mean/var fields."""
    n = struct.unpack_from('<I', data, 0)[0]
    off = 4
    nodes = []
    leaf_patches = []
    for i in range(n):
        idx = struct.unpack_from('<I', data, off)[0]; off += 4
        yc = struct.unpack_from('<i', data, off)[0]; off += 4
        if yc >= 0:
            nc = struct.unpack_from('<I', data, off)[0]; off += 4
            qi = struct.unpack_from('<I', data, off)[0]; off += 4
            nodes.append({'i': i, 't': 'B', 'idx': idx, 'yc': yc, 'nc': nc, 'qi': qi})
        else:
            unused = struct.unpack_from('<I', data, off)[0]; off += 4
            mean_off = off
            mean = struct.unpack_from('<f', data, off)[0]; off += 4
            var_off = off
            var = struct.unpack_from('<f', data, off)[0]; off += 4
            nodes.append({'i': i, 't': 'L', 'idx': idx, 'mean': mean, 'var': var,
                          'mean_off': mean_off, 'var_off': var_off})
            leaf_patches.append((mean_off, var_off))
    return nodes, leaf_patches


def find_tree_sub_chunks(chunk_data):
    """Find tree sub-chunks within an f0tr/durt container."""
    trees = []
    for tag, ds, sz in riff_chunks(chunk_data, 0):
        if tag == b'tree':
            trees.append((ds, sz))
    return trees


def parse_ques(data):
    """Parse ques sub-chunk data."""
    count = struct.unpack_from('<I', data, 0)[0]
    off = 4
    questions = []
    for _ in range(count):
        key = data[off]; off += 1
        vc = struct.unpack_from('<I', data, off)[0]; off += 4
        values = set()
        for _ in range(vc):
            values.add(struct.unpack_from('<I', data, off)[0]); off += 4
        questions.append((key, values))
    return questions


def find_ques_in_chunk(chunk_data):
    """Find and parse the ques sub-chunk within an f0tr/durt container."""
    for tag, ds, sz in riff_chunks(chunk_data, 0):
        if tag == b'ques':
            return parse_ques(chunk_data[ds:ds+sz])
        elif tag == b'trhd':
            for t2, d2, s2 in riff_chunks(chunk_data, ds, ds+sz):
                if t2 == b'ques':
                    return parse_ques(chunk_data[d2:d2+s2])
    raise ValueError("ques sub-chunk not found")


def traverse_tree(nodes, questions, features):
    """Traverse tree with given features, return leaf node index."""
    ni = 0
    while True:
        nd = nodes[ni]
        if nd['t'] == 'L':
            return ni
        qtype, qvalues = questions[nd['qi']]
        feat_val = features.get(qtype, 0)
        if feat_val in qvalues:
            ni = nd['yc']
        else:
            ni = nd['nc']


def load_unit_features(vin_data):
    """Load unit features needed for tree traversal from decoded VIN."""
    n_units = None
    for tag, ds, sz in riff_chunks(vin_data, 12):
        if tag == b'cnts':
            n_units = struct.unpack_from('<I', vin_data, ds + 8)[0]
            break
    assert n_units is not None, "cnts chunk not found"

    unit_data_ds = None
    for tag, ds, sz in riff_chunks(vin_data, 12):
        if tag == b'unit':
            for t2, d2, s2 in riff_chunks(vin_data, ds, ds + sz):
                if t2 == b'data' and s2 == n_units * UNIT_SIZE:
                    unit_data_ds = d2
                    break
            break
    assert unit_data_ds is not None, "unit data sub-chunk not found"

    syl_type = np.empty(n_units, dtype=np.uint8)
    syl_in_phrase = np.empty(n_units, dtype=np.uint8)
    word_in_phrase = np.empty(n_units, dtype=np.uint8)
    phone_in_syl = np.empty(n_units, dtype=np.uint8)
    phone_left = np.empty(n_units, dtype=np.uint8)
    phone_right = np.empty(n_units, dtype=np.uint8)
    pc = np.empty(n_units, dtype=np.uint8)
    dl = np.empty(n_units, dtype=np.uint16)
    f0_context = np.empty(n_units, dtype=np.uint8)

    for i in range(n_units):
        base = unit_data_ds + i * UNIT_SIZE
        dl[i] = struct.unpack_from('<H', vin_data, base + 0x0A)[0]
        syl_type[i] = vin_data[base + 0x0C]
        syl_in_phrase[i] = vin_data[base + 0x0D]
        word_in_phrase[i] = vin_data[base + 0x0E]
        phone_in_syl[i] = vin_data[base + 0x0F]
        f0_context[i] = vin_data[base + 0x13]
        pc[i] = vin_data[base + 0x14]
        phone_left[i] = vin_data[base + 0x17]
        phone_right[i] = vin_data[base + 0x18]

    return {
        'n_units': n_units,
        'syl_type': syl_type,
        'syl_in_phrase': syl_in_phrase,
        'word_in_phrase': word_in_phrase,
        'phone_in_syl': phone_in_syl,
        'phone_left': phone_left,
        'phone_right': phone_right,
        'pc': pc,
        'dl': dl,
        'f0_context': f0_context,
    }


# =========================================================================== #
#                                                                             #
#  STEP 1: Build Voice (VDB + VIN)                                            #
#                                                                             #
# =========================================================================== #
def step1_build_voice(voice_name, voice_dir, vin_out, vdb_out, tom_vin, tom_vdb,
                      wav_dir, tg_dir, n_workers):
    """Build <voice>8.vdb and <voice>.vin from WAVs + MFA alignment."""
    print("\n")
    print("=" * 70)
    print("STEP 1/5: Build voice (%s8.vdb + %s.vin)" % (voice_name, voice_name))
    print("=" * 70)

    state_file = os.path.join(voice_dir, 'build_state.pkl')
    N_UNITS = STEP1_N_UNITS

    # -- State cache helpers --
    def load_state_s1(tom_key):
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, 'rb') as f:
                state = pickle.load(f)
            if state.get('version') != STATE_VERSION:
                print("  State file version mismatch -- full rebuild.")
                return {}
            if state.get('tom_key') != tom_key:
                print("  tom.vin changed -- full rebuild.")
                return {}
            return state.get('recordings', {})
        except Exception as e:
            print("  Could not load state file (%s) -- full rebuild." % e)
            return {}

    def save_state_s1(tom_key, recordings):
        state = {
            'version':    STATE_VERSION,
            'tom_key':    tom_key,
            'recordings': recordings,
        }
        with open(state_file, 'wb') as f:
            pickle.dump(state, f, protocol=4)

    # -- 1. Parse tom.vin --
    print("Parsing tom.vin ...")
    vin = load_encoded(tom_vin)
    assert vin[:4] == b'RIFF' and vin[8:12] == b'svin', "Not a valid tom.vin"

    vin_chunks = {tag: (ds, sz) for tag, ds, sz in riff_chunks_12(vin)}

    feat_off, feat_sz = vin_chunks[b'feat']
    feat = vin[feat_off : feat_off + feat_sz]
    fn_idx = feat.find(b'filename')
    fn_count = struct.unpack_from('<I', feat, fn_idx + 8)[0]
    p = fn_idx + 12
    filenames = {}
    for _ in range(fn_count):
        nlen = struct.unpack_from('<H', feat, p)[0]
        name = feat[p+2 : p+2+nlen].decode('latin-1', errors='replace')
        stored_id = struct.unpack_from('<I', feat, p+2+nlen)[0]
        filenames[stored_id] = name
        p += 2 + nlen + 4
    print("  %d filenames in feat (%d distinct stored_ids)" % (fn_count, len(filenames)))

    unit_off, unit_sz = vin_chunks[b'unit']
    unit_data_ds = unit_data_sz = None
    for tag, ds, sz in riff_chunks_12(vin, unit_off):
        if tag == b'data':
            unit_data_ds, unit_data_sz = ds, sz
            break
    assert unit_data_ds is not None
    assert unit_data_sz == N_UNITS * UNIT_SIZE

    fidx_max_end = {}
    for i in range(N_UNITS):
        base = unit_data_ds + i * UNIT_SIZE
        fidx = struct.unpack_from('<H', vin, base +  4)[0]
        lp   = struct.unpack_from('<H', vin, base +  6)[0]
        dl   = struct.unpack_from('<H', vin, base + 10)[0]
        end  = lp + dl
        if end > fidx_max_end.get(fidx, 0):
            fidx_max_end[fidx] = end
    print("  %d units, %d distinct file_idx values" % (N_UNITS, len(fidx_max_end)))

    tom_key = _file_key(tom_vin)

    # -- 2. Parse tom8.vdb indx --
    print("Parsing tom8.vdb ...")
    vdb = load_encoded(tom_vdb)
    assert vdb[:4] == b'RIFF' and vdb[8:12] == b'WAVE'

    vdb_chunks = {tag: (ds, sz) for tag, ds, sz in riff_chunks_12(vdb)}
    data_ds, data_sz = vdb_chunks[b'data']
    indx_ds, indx_sz = vdb_chunks[b'indx']
    indx_end = indx_ds + indx_sz
    p = indx_ds + 4
    indx_entries = []
    while p <= indx_end - 6:
        off  = struct.unpack_from('<I', vdb, p)[0]
        nlen = struct.unpack_from('<H', vdb, p+4)[0]
        if p + 6 + nlen > indx_end:
            break
        name = vdb[p+6 : p+6+nlen].decode('latin-1', errors='replace')
        indx_entries.append((off, name))
        p += 6 + nlen
    assert indx_entries[-1][1] == ''

    tom_pcm = {}
    for i, (off, name) in enumerate(indx_entries[:-1]):
        next_off = indx_entries[i+1][0]
        sz = next_off - off
        if sz > 0 and name:
            tom_pcm[name] = vdb[data_ds + off : data_ds + off + sz]
    print("  %d indx entries, %d non-empty recordings" % (len(indx_entries)-1, len(tom_pcm)))

    # -- 3. Load and downsample WAVs --
    print("Loading WAVs (pass 1: downsample + spectral analysis) ...")
    if not os.path.isdir(wav_dir):
        print("  WARNING: WAV directory not found: %s" % wav_dir)
        wav_files = []
    else:
        wav_files = sorted(f for f in os.listdir(wav_dir) if f.lower().endswith('.wav'))
    voice_pcm16 = {}
    wav_mtimes = {}

    for fname in tqdm(wav_files, desc="WAVs", unit="file"):
        name = fname[:-4]
        path = os.path.join(wav_dir, fname)
        wav_mtimes[name] = _file_key(path)
        with wave.open(path) as w:
            n_ch      = w.getnchannels()
            sampwidth = w.getsampwidth()
            framerate = w.getframerate()
            raw_pcm   = w.readframes(w.getnframes())
        if n_ch != 1 or sampwidth != 2:
            tqdm.write("  SKIP %s: not mono 16-bit" % fname)
            continue
        if framerate == 24000:
            pcm_8k = downsample_3x(raw_pcm)
        elif framerate == 8000:
            pcm_8k = raw_pcm
        else:
            from scipy.signal import resample_poly
            g = math.gcd(framerate, 8000)
            up, down = 8000 // g, framerate // g
            samples = np.frombuffer(raw_pcm, dtype='<i2').copy().astype(np.float64)
            out = resample_poly(samples, up, down)
            pcm_8k = np.clip(out, -32768, 32767).astype('<i2').tobytes()
        voice_pcm16[name] = pcm_8k
    print("  %d recordings loaded" % len(voice_pcm16))

    # -- 3.1 Spectral envelope normalization --
    print("Computing spectral envelopes ...")
    rec_mel_spectra = {}
    for name, pcm in voice_pcm16.items():
        mel = compute_avg_mel_spectrum(pcm)
        if mel is not None:
            rec_mel_spectra[name] = mel

    if rec_mel_spectra:
        all_mels = np.array(list(rec_mel_spectra.values()))
        log_mels = np.log(all_mels + 1e-10)
        global_target_mel = np.exp(np.mean(log_mels, axis=0))
        gains_db_all = []
        for name, rm in rec_mel_spectra.items():
            g = np.sqrt((global_target_mel + 1e-10) / (rm + 1e-10))
            g = np.clip(g, 10.0 ** (-SPEC_MAX_DB / 20.0), 10.0 ** (SPEC_MAX_DB / 20.0))
            gains_db_all.append(20.0 * np.log10(g))
        gains_db_arr = np.array(gains_db_all)
        print("  %d recordings analyzed (%d mel bands)" % (len(rec_mel_spectra), SPEC_N_MELS))
        print("  Per-band correction range: [%.1f, %.1f] dB" % (gains_db_arr.min(), gains_db_arr.max()))
        print("  Mean abs correction: %.1f dB" % np.mean(np.abs(gains_db_arr)))
    else:
        global_target_mel = None
        print("  No recordings long enough for spectral analysis -- skipping EQ")

    print("Applying spectral EQ + RMS normalization + mu-law encoding (pass 2) ...")
    voice_pcm = {}
    eq_applied = 0
    eq_skipped = 0
    for name, pcm in tqdm(voice_pcm16.items(), desc="EQ+encode", unit="rec"):
        if global_target_mel is not None and name in rec_mel_spectra:
            pcm = spectral_eq(pcm, rec_mel_spectra[name], global_target_mel)
            eq_applied += 1
        else:
            eq_skipped += 1
        voice_pcm[name] = pcm16_to_ulaw(normalize_rms(pcm))
    del voice_pcm16
    print("  Spectral EQ: %d applied, %d skipped (too short)" % (eq_applied, eq_skipped))
    print("  %d recordings ready" % len(voice_pcm))

    # -- 3.5. Load MFA TextGrid alignments --
    print("Loading MFA TextGrid alignments ...")
    if os.path.isdir(tg_dir):
        tg_files = sorted(f for f in os.listdir(tg_dir) if f.endswith('.TextGrid'))
    else:
        tg_files = []
    tg_data   = {}
    tg_mtimes = {}

    for fname in tqdm(tg_files, desc="TextGrids", unit="file"):
        name = fname[:-9]
        path = os.path.join(tg_dir, fname)
        tg_mtimes[name] = _file_key(path)
        ivs, xmax = parse_textgrid_all(path)
        if ivs and xmax > 0.0:
            tg_data[name] = (ivs, xmax)
    print("  %d TextGrids loaded" % len(tg_data))

    # -- 4. Build VDB --
    print("Building %s8.vdb ..." % voice_name)
    regular = indx_entries[:-1]

    new_entry_pcm  = []
    name_n_samples = {}

    for _, name in regular:
        if name in voice_pcm:
            pcm = voice_pcm[name]
        elif name in tom_pcm:
            pcm = tom_pcm[name]
        else:
            pcm = b''
        new_entry_pcm.append(pcm)
        if name:
            name_n_samples[name] = len(pcm)

    data_buf = io.BytesIO()
    new_indx = []
    for i, (_, name) in enumerate(regular):
        new_indx.append((data_buf.tell(), name))
        data_buf.write(new_entry_pcm[i])
    new_indx.append((data_buf.tell(), ''))
    new_data_bytes = data_buf.getvalue()
    new_data_bytes = new_data_bytes + b'\xff' * WSOLA_PAD

    indx_buf = io.BytesIO()
    indx_buf.write(struct.pack('<I', len(new_indx)))
    for off, name in new_indx:
        name_enc = name.encode('latin-1')
        indx_buf.write(struct.pack('<I', off))
        indx_buf.write(struct.pack('<H', len(name_enc)))
        indx_buf.write(name_enc)
    new_indx_bytes = indx_buf.getvalue()

    def extract_chunk(data, tag_ds, tag_sz):
        return data[tag_ds-8 : tag_ds-8 + 8 + tag_sz + (tag_sz & 1)]

    list_ds, list_sz = vdb_chunks[b'LIST']
    fmt_ds,  fmt_sz  = vdb_chunks[b'fmt ']
    riff_body = (
        b'WAVE' +
        extract_chunk(vdb, list_ds, list_sz) +
        extract_chunk(vdb, fmt_ds,  fmt_sz)  +
        pack_chunk(b'indx', new_indx_bytes) +
        pack_chunk(b'data', new_data_bytes)
    )
    out_vdb = b'RIFF' + struct.pack('<I', len(riff_body)) + riff_body
    with open(vdb_out, 'wb') as f:
        f.write(xor_codec(out_vdb))
    print("  Wrote %s  (%d bytes)" % (vdb_out, len(out_vdb)))

    # -- 5. Build new unit/data bytes --
    print("Building unit table ...")
    cached_recs = load_state_s1(tom_key)

    units_by_fidx = defaultdict(list)
    for i in range(N_UNITS):
        base     = unit_data_ds + i * UNIT_SIZE
        fidx     = struct.unpack_from('<H', vin, base +  4)[0]
        lp       = struct.unpack_from('<H', vin, base +  6)[0]
        dl       = struct.unpack_from('<H', vin, base + 10)[0]
        is_first = vin[base + 21]
        pc       = vin[base + 20]
        units_by_fidx[fidx].append((i, lp, dl, is_first, pc))
    for fidx in units_by_fidx:
        units_by_fidx[fidx].sort(key=lambda x: x[1])

    new_unit_data = {}
    new_cached_recs = {}

    cache_hits = cache_misses = 0
    tg_count = fuzzy_count = spn_count = prop_count = energy_count = zero_count = 0
    mfa_aligned_uids = set()
    f0_ok = f0_fail = f0_tom_fallback = 0

    # -- Phase A: apply cache hits, collect cache misses --
    miss_tasks = []
    for fidx, units in units_by_fidx.items():
        rec_name    = filenames.get(fidx, '')
        tom_max_end = fidx_max_end.get(fidx, 0)
        mara_n      = name_n_samples.get(rec_name, 0)
        cap         = mara_n // 8 - 1 if mara_n >= 16 else 0
        rec_cache_key = (wav_mtimes.get(rec_name), tg_mtimes.get(rec_name))

        ivs, xmax_rec = tg_data.get(rec_name, (None, 0.0))

        cached = cached_recs.get(rec_name)
        if cached and cached.get('key') == rec_cache_key:
            for entry in cached['units']:
                new_unit_data[entry[0]] = tuple(entry[1:])
            new_cached_recs[rec_name] = cached
            cache_hits += 1
            if mara_n == 0:
                zero_count += len(units)
            elif ivs and any(lbl not in _REAL_PHONE_SET for _, _, lbl in ivs):
                tg_count += len(units)
                for entry in cached['units']:
                    mfa_aligned_uids.add(entry[0])
            elif ivs and any(l.strip().lower() == 'spn' for _, _, l in ivs):
                spn_count += len(units)
            else:
                prop_count += len(units)
        else:
            cache_misses += 1
            miss_tasks.append((fidx, units, rec_name, tom_max_end, mara_n, cap,
                               ivs, xmax_rec, rec_cache_key))

    # -- Phase B: process cache misses in parallel --
    print("Building unit table (cache: %d hits, %d misses) ..." % (cache_hits, cache_misses))
    if miss_tasks:
        print("  Spawning %d worker thread(s) for %d recordings ..." % (n_workers, cache_misses))

        def _submit(task):
            _, units, rec_name, tom_max_end, mara_n, cap, ivs, xmax_rec, _ = task
            ulaw_bytes = voice_pcm.get(rec_name)
            return process_recording(rec_name, units, tom_max_end, mara_n, cap,
                                     ulaw_bytes, ivs, xmax_rec)

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            future_to_task = {pool.submit(_submit, t): t for t in miss_tasks}
            for future in tqdm(as_completed(future_to_task), total=len(miss_tasks),
                               desc="Processing", unit="rec"):
                task = future_to_task[future]
                _, units, rec_name, tom_max_end, mara_n, cap, ivs, xmax_rec, rec_cache_key = task
                rec_units_out, f0_status, mfa_mode = future.result()

                for entry in rec_units_out:
                    new_unit_data[entry[0]] = entry[1:]
                new_cached_recs[rec_name] = {'key': rec_cache_key, 'units': rec_units_out}

                if mara_n == 0:
                    zero_count += len(rec_units_out)
                elif mfa_mode == 1:
                    tg_count += len(rec_units_out)
                    for entry in rec_units_out:
                        mfa_aligned_uids.add(entry[0])
                elif mfa_mode == 2:
                    fuzzy_count += len(rec_units_out)
                    for entry in rec_units_out:
                        mfa_aligned_uids.add(entry[0])
                elif mfa_mode == 3:
                    spn_count += len(rec_units_out)
                elif mfa_mode == 4:
                    energy_count += len(rec_units_out)
                else:
                    prop_count += len(rec_units_out)
                if f0_status == 1:
                    f0_ok += 1
                elif f0_status == -1:
                    f0_fail += 1

    print("  MFA seq exact:     %d units  (%d%%)" % (tg_count, 100*tg_count//N_UNITS))
    print("  MFA seq aligned:   %d units  (%d%%)" % (fuzzy_count, 100*fuzzy_count//N_UNITS))
    print("  SPN-region prop:   %d units  (%d%%)" % (spn_count, 100*spn_count//N_UNITS))
    print("  Energy-region:     %d units  (%d%%)" % (energy_count, 100*energy_count//N_UNITS))
    print("  Proportional:      %d units  (%d%%)" % (prop_count, 100*prop_count//N_UNITS))
    print("  Zeroed (no audio): %d units" % zero_count)
    n_disabled = sum(1 for uid in new_unit_data if new_unit_data[uid][1] == 0)
    print("  Disabled (unmatched phone): %d units" % n_disabled)
    if cache_misses:
        print("  F0: %d computed, %d failed/skipped" % (f0_ok, f0_fail))

    # -- Post-processing: inflate dl --
    print("Inflating dl values toward Tom's originals ...")
    dl_inflated = 0
    dl_already_ok = 0
    dl_total_gain = 0
    for fidx, units in units_by_fidx.items():
        rec_name = filenames.get(fidx, '')
        mara_n = name_n_samples.get(rec_name, 0)
        cap = mara_n // 8 - 1 if mara_n >= 16 else 0
        for uid, tom_lp, tom_dl, is_first, pc in units:
            if uid not in new_unit_data:
                continue
            if pc in _SILENCE_PCS:
                continue
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[uid]
            if new_dl == 0:
                continue
            if cap > 0 and tom_dl > 0 and new_lp >= cap - MIN_DL_FLOOR:
                new_lp = max(0, cap - tom_dl)
            target_dl = min(tom_dl, max(0, cap - new_lp))
            target_dl = max(target_dl, MIN_DL_FLOOR)
            target_dl = min(target_dl, max(0, cap - new_lp))
            if target_dl > new_dl:
                dl_total_gain += target_dl - new_dl
                new_unit_data[uid] = (new_lp, target_dl, new_pc, new_f0s, new_f0e, new_f0m)
                dl_inflated += 1
            else:
                dl_already_ok += 1
    print("  %d units inflated (avg gain: %.1f dl units = %.1f ms)" % (
        dl_inflated,
        dl_total_gain / max(1, dl_inflated),
        dl_total_gain * 0.5 / max(1, dl_inflated)))
    print("  %d units already at or above Tom's dl" % dl_already_ok)

    # -- Post-processing: close LP gaps --
    print("Closing LP gaps within same-recording unit groups ...")
    lp_gaps_closed = 0
    lp_gap_total_reduction = 0
    for fidx, units in units_by_fidx.items():
        rec_uids = []
        for uid, tom_lp, tom_dl, is_first, pc in units:
            if uid not in new_unit_data:
                continue
            rec_uids.append((uid, tom_lp))
        if len(rec_uids) < 2:
            continue
        rec_uids.sort(key=lambda x: x[1])

        prev_end = -1
        for uid, _ in rec_uids:
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[uid]
            if new_dl == 0:
                continue
            if uid in mfa_aligned_uids:
                prev_end = new_lp + new_dl
                continue
            if prev_end < 0:
                prev_end = new_lp + new_dl
                continue
            gap = new_lp - prev_end
            if gap > MAX_LP_GAP:
                new_lp = prev_end
                new_unit_data[uid] = (new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m)
                lp_gaps_closed += 1
                lp_gap_total_reduction += gap
            prev_end = new_unit_data[uid][0] + new_unit_data[uid][1]
    print("  %d gaps closed (avg reduction: %.1f lp units)" % (
        lp_gaps_closed, lp_gap_total_reduction / max(1, lp_gaps_closed)))

    # -- Final monotonicity + minimum spacing --
    print("Final monotonicity + minimum spacing enforcement ...")
    _mono_fixes = 0
    for fidx, units in units_by_fidx.items():
        rec_name = filenames.get(fidx, '')
        mara_n_loc = name_n_samples.get(rec_name, 0)
        cap = mara_n_loc // 8 - 1 if mara_n_loc >= 16 else 0
        active = []
        for uid, tom_lp, tom_dl, is_first, pc in units:
            if uid not in new_unit_data:
                continue
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[uid]
            if new_dl > 0:
                active.append((tom_lp, uid))
        if len(active) < 2:
            continue
        active.sort()
        prev_lp = -(MIN_LP_SPACING + 1)
        for _, uid in active:
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[uid]
            min_lp = prev_lp + MIN_LP_SPACING
            if new_lp < min_lp:
                new_lp = min(min_lp, cap)
                new_dl = max(1, min(cap - new_lp, new_dl))
                new_unit_data[uid] = (new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m)
                _mono_fixes += 1
            prev_lp = new_lp
    print("  %d units adjusted for monotonicity/spacing" % _mono_fixes)

    # -- Final safety clamp --
    _oob_fixes = 0
    for fidx, units in units_by_fidx.items():
        rec_name = filenames.get(fidx, '')
        mara_n_loc = name_n_samples.get(rec_name, 0)
        cap = mara_n_loc // 8 - 1 if mara_n_loc >= 16 else 0
        if cap <= 0:
            continue
        for uid, tom_lp, tom_dl, is_first, pc in units:
            if uid not in new_unit_data:
                continue
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[uid]
            orig_lp, orig_dl = new_lp, new_dl
            new_lp = max(0, min(new_lp, cap))
            max_dl = cap - new_lp
            new_dl = max(1, min(new_dl, max_dl))
            if new_lp != orig_lp or new_dl != orig_dl:
                new_unit_data[uid] = (new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m)
                _oob_fixes += 1
    print("  %d units clamped to recording bounds (safety net)" % _oob_fixes)

    # Pack unit buffer
    new_unit_buf = bytearray(N_UNITS * UNIT_SIZE)
    for i in tqdm(range(N_UNITS), desc="Packing units", unit="unit", miniters=10000):
        src_base = unit_data_ds + i * UNIT_SIZE
        dst_base = i * UNIT_SIZE
        rec = bytearray(vin[src_base : src_base + UNIT_SIZE])
        if i in new_unit_data:
            new_lp, new_dl, new_pc, new_f0s, new_f0e, new_f0m = new_unit_data[i]
            struct.pack_into('<H', rec,  6, min(new_lp, 0xFFFF))
            struct.pack_into('<H', rec, 10, min(new_dl, 0xFFFF))
            if new_f0s >= 0:
                fell_back = False
                if new_f0s == 0 and rec[16] > 0:
                    fell_back = True
                else:
                    rec[16] = new_f0s
                if new_f0e == 0 and rec[17] > 0:
                    fell_back = True
                else:
                    rec[17] = new_f0e
                if new_f0m == 0 and rec[18] > 0:
                    fell_back = True
                else:
                    rec[18] = new_f0m
                if fell_back:
                    f0_tom_fallback += 1
            if new_pc != 255:
                rec[20] = new_pc
        new_unit_buf[dst_base : dst_base + UNIT_SIZE] = rec

    if f0_tom_fallback:
        print("  F0 Tom fallback: %d units kept Tom's f0 (HARVEST unvoiced, Tom voiced)" % f0_tom_fallback)

    # -- 6. Build VIN --
    print("Building %s.vin ..." % voice_name)
    new_vin = (vin[:unit_data_ds]
               + bytes(new_unit_buf)
               + vin[unit_data_ds + N_UNITS * UNIT_SIZE:])
    with open(vin_out, 'wb') as f:
        f.write(xor_codec(new_vin))
    print("  Wrote %s" % vin_out)

    # -- 7. Persist state --
    print("Saving build state ...")
    save_state_s1(tom_key, new_cached_recs)
    print("  %d recordings cached to %s" % (len(new_cached_recs), state_file))

    # -- 8. Clear cache --
    print("Clearing %s voice cache ..." % voice_name)
    tmpdir = os.environ.get('TMPDIR', os.environ.get('TEMP', tempfile.gettempdir()))
    cleared = 0
    for path in _glob.glob(os.path.join(tmpdir, 'cache_%s_8_*' % voice_name)):
        try:
            shutil.rmtree(path)
            cleared += 1
        except Exception as e:
            print("  Warning: could not remove %s: %s" % (path, e))
    if cleared == 0:
        print("  (no cache entries found)")

    print("\nStep 1 done.")
    print("  VDB: %s" % vdb_out)
    print("  VIN: %s" % vin_out)
    print("=" * 70)


# =========================================================================== #
#                                                                             #
#  STEP 2: Build Extras                                                       #
#                                                                             #
# =========================================================================== #
def step2_build_extras(voice_name, voice_dir, vin_path, vdb_path, tom_vin_path, n_workers):
    """Replace low-quality units with extra recording data."""
    print("\n")
    print("=" * 70)
    print("STEP 2/5: Build extras (REPLACE strategy)")
    print("=" * 70)

    extra_wav_dir = os.path.join(voice_dir, 'output', 'extra_wavs')
    extra_tg_dir  = os.path.join(voice_dir, 'output', 'extra_tg')
    state_file    = os.path.join(voice_dir, 'build_extra_state.pkl')

    # -- State cache helpers --
    def load_state_s2():
        if not os.path.exists(state_file):
            return {}
        try:
            with open(state_file, 'rb') as f:
                state = pickle.load(f)
            if state.get('version') != STATE_VERSION:
                print("  State file version mismatch -- full rebuild.")
                return {}
            return state.get('recordings', {})
        except Exception as e:
            print("  Could not load state file (%s) -- full rebuild." % e)
            return {}

    def save_state_s2(recordings):
        state = {
            'version':    STATE_VERSION,
            'recordings': recordings,
        }
        with open(state_file, 'wb') as f:
            pickle.dump(state, f, protocol=4)

    # -- 1. Find extra recordings --
    if not os.path.isdir(extra_wav_dir):
        print("\nNo extra_wavs directory found at: %s" % extra_wav_dir)
        print("Create this directory and add WAV files to add extra recordings.")
        return
    wav_names = sorted(
        f[:-4] for f in os.listdir(extra_wav_dir)
        if f.lower().endswith('.wav')
    )
    if not wav_names:
        print("\nNo WAV files found in %s. Nothing to do." % extra_wav_dir)
        return

    tg_dir_exists = os.path.isdir(extra_tg_dir)
    tg_available = set()
    if tg_dir_exists:
        tg_available = set(
            f[:-9] for f in os.listdir(extra_tg_dir)
            if f.endswith('.TextGrid')
        )

    matched = [n for n in wav_names if n in tg_available]
    unmatched = [n for n in wav_names if n not in tg_available]
    print("\n  WAVs found:      %d" % len(wav_names))
    print("  TextGrids found: %d" % len(tg_available))
    print("  Matched pairs:   %d" % len(matched))
    if unmatched:
        print("  Unmatched (skipped): %d" % len(unmatched))

    if not matched:
        print("\nNo matched WAV+TextGrid pairs. Run MFA on extra_wavs first.")
        return

    # -- 2. Fit f0_context regression from Tom --
    print("\nFitting f0_context regression from Tom ...")
    tom_vin = xor_decode(tom_vin_path)
    f0ctx_fn = fit_f0_context(tom_vin)

    # -- 3. Load existing VIN --
    print("\nLoading %s.vin ..." % voice_name)
    vin_data = bytearray(xor_decode(vin_path))

    vin_chunk_list = []
    for tag, ds, sz in riff_chunks(vin_data, 12):
        vin_chunk_list.append((tag, ds, sz))

    cnts_ds = cnts_sz = None
    for tag, ds, sz in vin_chunk_list:
        if tag == b'cnts':
            cnts_ds, cnts_sz = ds, sz
            break
    assert cnts_ds is not None, "cnts chunk not found"
    current_n_units = struct.unpack_from('<I', vin_data, cnts_ds + 8)[0]
    print("  Current unit count: %d" % current_n_units)

    feat_ds = feat_sz = None
    for tag, ds, sz in vin_chunk_list:
        if tag == b'feat':
            feat_ds, feat_sz = ds, sz
            break
    assert feat_ds is not None, "feat chunk not found"
    feat_body = bytes(vin_data[feat_ds:feat_ds + feat_sz])

    fn_idx = feat_body.find(b'filename')
    assert fn_idx >= 0, "filename key not found in feat"
    fn_count = struct.unpack_from('<I', feat_body, fn_idx + 8)[0]
    p = fn_idx + 12
    existing_names = set()
    max_stored_id = -1
    for _ in range(fn_count):
        nlen = struct.unpack_from('<H', feat_body, p)[0]
        name = feat_body[p+2:p+2+nlen].decode('latin-1', errors='replace')
        sid = struct.unpack_from('<I', feat_body, p+2+nlen)[0]
        existing_names.add(name)
        if sid > max_stored_id:
            max_stored_id = sid
        p += 2 + nlen + 4
    fn_section_end = p
    print("  Existing filenames: %d (max stored_id=%d)" % (fn_count, max_stored_id))

    unit_ds = unit_sz = None
    for tag, ds, sz in vin_chunk_list:
        if tag == b'unit':
            unit_ds, unit_sz = ds, sz
            break
    assert unit_ds is not None, "unit chunk not found"
    unit_data_ds = unit_data_sz = None
    for tag, ds, sz in riff_chunks(vin_data, unit_ds, unit_ds + unit_sz):
        if tag == b'data':
            unit_data_ds, unit_data_sz = ds, sz
            break
    assert unit_data_ds is not None, "unit data sub-chunk not found"
    assert unit_data_sz == current_n_units * UNIT_SIZE, \
        "unit data size mismatch: %d != %d" % (unit_data_sz, current_n_units * UNIT_SIZE)

    # -- 4. Load existing VDB --
    print("\nLoading %s8.vdb ..." % voice_name)
    vdb_data = xor_decode(vdb_path)

    vdb_chunks = {}
    for tag, ds, sz in riff_chunks(vdb_data, 12):
        vdb_chunks[tag] = (ds, sz)

    vdb_data_ds, vdb_data_sz = vdb_chunks[b'data']
    vdb_indx_ds, vdb_indx_sz = vdb_chunks[b'indx']

    vdb_indx_count = struct.unpack_from('<I', vdb_data, vdb_indx_ds)[0]
    vp = vdb_indx_ds + 4
    vdb_entries = []
    for _ in range(vdb_indx_count):
        off = struct.unpack_from('<I', vdb_data, vp)[0]
        nlen = struct.unpack_from('<H', vdb_data, vp + 4)[0]
        name = vdb_data[vp+6:vp+6+nlen].decode('latin-1', errors='replace')
        vdb_entries.append((off, name))
        vp += 6 + nlen
    vdb_existing_names = set(name for _, name in vdb_entries if name)
    sentinel_off = vdb_entries[-1][0]
    print("  VDB entries: %d (data up to byte %d)" % (len(vdb_entries) - 1, sentinel_off))

    vdb_list_ds, vdb_list_sz = vdb_chunks[b'LIST']
    vdb_fmt_ds, vdb_fmt_sz = vdb_chunks[b'fmt ']
    vdb_list_chunk = vdb_data[vdb_list_ds-8 : vdb_list_ds + vdb_list_sz + (vdb_list_sz & 1)]
    vdb_fmt_chunk = vdb_data[vdb_fmt_ds-8 : vdb_fmt_ds + vdb_fmt_sz + (vdb_fmt_sz & 1)]

    # -- 5. Compute run_potential --
    print("\nComputing run_potential for %d units ..." % current_n_units)
    run_potential = compute_run_potential_extras(vin_data, unit_data_ds, current_n_units)
    active_count = int(np.sum(run_potential >= 0))
    print("  Active units (dl>0): %d" % active_count)
    print("  Run potential stats (active): min=%d, median=%d, mean=%.1f, max=%d" % (
        int(np.min(run_potential[run_potential >= 0])),
        int(np.median(run_potential[run_potential >= 0])),
        float(np.mean(run_potential[run_potential >= 0])),
        int(np.max(run_potential[run_potential >= 0])),
    ))

    # -- 6. Build replacement pools --
    print("\nBuilding replacement pools by (phone_center, is_first_half) ...")
    replacement_pools = defaultdict(list)
    for uid in range(current_n_units):
        rp = run_potential[uid]
        if rp < 0:
            continue
        base = unit_data_ds + uid * UNIT_SIZE
        pc = vin_data[base + 20]
        is_first = vin_data[base + 21]
        replacement_pools[(pc, is_first)].append((rp, uid))

    for key in replacement_pools:
        replacement_pools[key].sort(key=lambda x: x[0])

    pool_sizes = {k: len(v) for k, v in replacement_pools.items()}
    total_pool = sum(pool_sizes.values())
    print("  %d distinct (pc, is_first) groups, %d total replaceable units" % (
        len(pool_sizes), total_pool))

    # -- 7. Process extra recordings --
    to_process = []
    skip_existing = 0
    for name in matched:
        if name in vdb_existing_names:
            skip_existing += 1
        else:
            to_process.append(name)
    if skip_existing:
        print("  Skipped %d recordings already in VDB" % skip_existing)
    if not to_process:
        print("\nAll extra recordings already present in VDB. Nothing to do.")
        return
    print("\n  Processing %d new recordings ..." % len(to_process))

    cached_recs = load_state_s2()
    new_cached_recs = {}

    wav_keys = {}
    tg_keys = {}
    for name in to_process:
        wav_keys[name] = _file_key(os.path.join(extra_wav_dir, name + '.wav'))
        tg_keys[name] = _file_key(os.path.join(extra_tg_dir, name + '.TextGrid'))

    cache_hits = 0
    cache_misses = 0
    miss_names = []
    hit_results = {}

    for name in to_process:
        rec_cache_key = (wav_keys[name], tg_keys[name])
        cached = cached_recs.get(name)
        if cached and cached.get('key') == rec_cache_key:
            hit_results[name] = cached
            new_cached_recs[name] = cached
            cache_hits += 1
        else:
            miss_names.append(name)
            cache_misses += 1

    print("  Cache: %d hits, %d misses" % (cache_hits, cache_misses))

    miss_results = {}
    if miss_names:
        print("  Spawning %d worker thread(s) for %d recordings ..." % (
            n_workers, cache_misses))

        def _submit_extra(name):
            wav_path = os.path.join(extra_wav_dir, name + '.wav')
            tg_path = os.path.join(extra_tg_dir, name + '.TextGrid')
            return process_extra_recording(name, wav_path, tg_path, f0ctx_fn)

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            future_to_name = {pool.submit(_submit_extra, n): n for n in miss_names}
            for future in tqdm(as_completed(future_to_name), total=len(miss_names),
                               desc="Processing", unit="rec"):
                name = future_to_name[future]
                result = future.result()
                if result is None:
                    tqdm.write("  SKIP %s" % name)
                    continue
                ulaw, unit_fields, f0_status = result
                rec_cache_key = (wav_keys[name], tg_keys[name])
                entry = {
                    'key': rec_cache_key,
                    'ulaw': ulaw,
                    'fields': unit_fields,
                    'f0': f0_status,
                }
                miss_results[name] = entry
                new_cached_recs[name] = entry

    # -- 8. Assign replacements --
    print("\nAssigning replacement targets ...")
    pool_cursors = {k: 0 for k in replacement_pools}

    def pop_replacement(pc, is_first):
        key = (pc, is_first)
        ppool = replacement_pools.get(key)
        if ppool is None:
            return None
        cursor = pool_cursors[key]
        if cursor >= len(ppool):
            return None
        _, uid = ppool[cursor]
        pool_cursors[key] = cursor + 1
        return uid

    replacement_groups = []
    total_replaced = 0
    total_skipped_no_target = 0
    f0_ok = 0
    f0_fail = 0

    new_vdb_audio = io.BytesIO()
    new_vdb_entries = []
    new_feat_entries = []
    next_stored_id = max_stored_id + 1

    for name in to_process:
        entry = hit_results.get(name) or miss_results.get(name)
        if entry is None:
            continue

        ulaw = entry['ulaw']
        unit_fields = entry['fields']
        f0_status = entry['f0']

        if f0_status == 1:
            f0_ok += 1
        else:
            f0_fail += 1

        replacements = []
        for fields_tuple in unit_fields:
            pc = fields_tuple[2]
            is_first = fields_tuple[3]
            target_uid = pop_replacement(pc, is_first)
            if target_uid is not None:
                replacements.append((target_uid, fields_tuple))
            else:
                total_skipped_no_target += 1

        if not replacements:
            continue

        file_idx = next_stored_id

        vdb_offset = sentinel_off + new_vdb_audio.tell()
        new_vdb_audio.write(ulaw)
        new_vdb_entries.append((vdb_offset, name))

        new_feat_entries.append((name, next_stored_id))
        next_stored_id += 1

        replacement_groups.append((name, file_idx, replacements))
        total_replaced += len(replacements)

    new_vdb_bytes = new_vdb_audio.getvalue()
    n_new_recs = len(new_vdb_entries)

    print("  Recordings with replacements: %d" % n_new_recs)
    print("  Units replaced:               %d" % total_replaced)
    print("  Units skipped (no target):    %d" % total_skipped_no_target)
    print("  F0 extraction: %d ok, %d failed" % (f0_ok, f0_fail))

    if n_new_recs == 0:
        print("\nNo replacements possible. Nothing to write.")
        return

    # -- 9. Apply replacements to unit records --
    print("\nApplying %d unit replacements to %s.vin ..." % (total_replaced, voice_name))

    for name, file_idx, replacements in replacement_groups:
        for target_uid, fields_tuple in replacements:
            lp, dl, pc, is_first, f0s, f0e, f0m, f0ctx = fields_tuple
            base = unit_data_ds + target_uid * UNIT_SIZE

            struct.pack_into('<H', vin_data, base + 4, file_idx)
            struct.pack_into('<H', vin_data, base + 6, min(lp, 0xFFFF))
            struct.pack_into('<H', vin_data, base + 10, min(dl, 0xFFFF))
            vin_data[base + 16] = f0s
            vin_data[base + 17] = f0e
            vin_data[base + 18] = f0m
            vin_data[base + 19] = f0ctx

    # -- 10. Rebuild VDB --
    print("\nRebuilding %s8.vdb ..." % voice_name)

    existing_data = vdb_data[vdb_data_ds:vdb_data_ds + sentinel_off]
    combined_data = existing_data + new_vdb_bytes + (b'\xff' * WSOLA_PAD)

    all_entries = list(vdb_entries[:-1])
    all_entries.extend(new_vdb_entries)
    new_sentinel_off = sentinel_off + len(new_vdb_bytes)
    all_entries.append((new_sentinel_off, ''))

    indx_buf = io.BytesIO()
    indx_buf.write(struct.pack('<I', len(all_entries)))
    for off, nm in all_entries:
        nm_enc = nm.encode('latin-1')
        indx_buf.write(struct.pack('<I', off))
        indx_buf.write(struct.pack('<H', len(nm_enc)))
        indx_buf.write(nm_enc)

    vdb_body = (
        b'WAVE' +
        vdb_list_chunk +
        vdb_fmt_chunk +
        pack_chunk(b'indx', indx_buf.getvalue()) +
        pack_chunk(b'data', combined_data)
    )
    new_vdb = b'RIFF' + struct.pack('<I', len(vdb_body)) + vdb_body
    with open(vdb_path, 'wb') as f:
        f.write(xor_encode(new_vdb))
    print("  Wrote %s (%d bytes, %d total recordings)" % (
        vdb_path, len(new_vdb), len(all_entries) - 1))

    # -- 11. Rebuild VIN --
    print("\nRebuilding %s.vin ..." % voice_name)

    new_fn_entries_buf = bytearray()
    for name, sid in new_feat_entries:
        nm_enc = name.encode('latin-1')
        new_fn_entries_buf.extend(struct.pack('<H', len(nm_enc)))
        new_fn_entries_buf.extend(nm_enc)
        new_fn_entries_buf.extend(struct.pack('<I', sid))

    new_fn_count = fn_count + len(new_feat_entries)
    new_feat_body = bytearray(feat_body)
    struct.pack_into('<I', new_feat_body, fn_idx + 8, new_fn_count)
    new_feat_body = (bytes(new_feat_body[:fn_section_end]) +
                     bytes(new_fn_entries_buf) +
                     bytes(new_feat_body[fn_section_end:]))

    modified_unit_data = bytes(vin_data[unit_data_ds:unit_data_ds + unit_data_sz])

    vin_body = bytearray()
    vin_body.extend(b'svin')

    for tag, ds, sz in vin_chunk_list:
        if tag == b'feat':
            vin_body.extend(pack_chunk(b'feat', bytes(new_feat_body)))
        elif tag == b'unit':
            prefix = vin_data[ds:unit_data_ds - 8]
            unit_inner = prefix + pack_chunk(b'data', modified_unit_data)
            vin_body.extend(pack_chunk(b'unit', unit_inner))
        else:
            chunk_start = ds - 8
            chunk_len = 8 + sz + (sz & 1)
            vin_body.extend(vin_data[chunk_start:chunk_start + chunk_len])

    new_vin = b'RIFF' + struct.pack('<I', len(vin_body)) + bytes(vin_body)
    with open(vin_path, 'wb') as f:
        f.write(xor_encode(new_vin))
    print("  Wrote %s (%d bytes, %d units unchanged, %d filenames)" % (
        vin_path, len(new_vin), current_n_units, new_fn_count))

    # -- 12. Save build cache --
    save_state_s2(new_cached_recs)
    print("  %d recordings cached to %s" % (len(new_cached_recs), state_file))

    # -- Summary --
    print("Step 2 done. REPLACE strategy results:")
    print("  Recordings added to VDB: %d" % n_new_recs)
    print("  Filenames added to feat: %d" % len(new_feat_entries))
    print("  Units REPLACED:          %d (of %d total)" % (total_replaced, current_n_units))
    print("  Units skipped (no pool): %d" % total_skipped_no_target)
    print("  Unit count:              %d (unchanged)" % current_n_units)
    print("=" * 70)


# =========================================================================== #
#                                                                             #
#  STEP 3: Build Rest (hash + prsl + mean patches)                            #
#                                                                             #
# =========================================================================== #
def step3_build_rest(voice_name, vin_path, tom_vin_path):
    """Rebuild hash, prsl, and mean chunks in the voice VIN."""
    print("\n")
    print("=" * 70)
    print("STEP 3/5: Build rest (hash + prsl + mean patches)")
    print("=" * 70)

    print("\nLoading %s.vin ..." % voice_name)
    vin = bytearray(xor_decode(vin_path))
    assert bytes(vin[:4]) == b'RIFF' and bytes(vin[8:12]) == b'svin'
    vin_chunks = {tag: (ds, sz) for tag, ds, sz in riff_chunks(vin, start=12)}

    # -- filenames --
    feat_ds, feat_sz = vin_chunks[b'feat']
    feat_data = bytes(vin[feat_ds : feat_ds + feat_sz])
    fn_idx   = feat_data.find(b'filename')
    fn_count = struct.unpack_from('<I', feat_data, fn_idx + 8)[0]
    p = fn_idx + 12
    filenames = {}
    for _ in range(fn_count):
        nlen = struct.unpack_from('<H', feat_data, p)[0]
        name = feat_data[p+2:p+2+nlen].decode('latin-1', errors='replace')
        sid  = struct.unpack_from('<I', feat_data, p+2+nlen)[0]
        filenames[sid] = name
        p += 2 + nlen + 4
    print("  %d filenames (%d distinct stored_ids)" % (fn_count, len(filenames)))

    # -- cnts --
    cnts_ds, cnts_sz = vin_chunks[b'cnts']
    N_UNITS = struct.unpack_from('<I', vin, cnts_ds + 8)[0]

    # -- unit table --
    unit_ds, unit_sz = vin_chunks[b'unit']
    unit_data_ds = unit_data_sz = None
    for tag, ds, sz in riff_chunks(vin, start=unit_ds, end=unit_ds+unit_sz):
        if tag == b'data':
            unit_data_ds, unit_data_sz = ds, sz
            break
    assert unit_data_ds is not None and unit_data_sz == N_UNITS * UNIT_SIZE

    uid_fidx = np.empty(N_UNITS, dtype=np.uint16)
    uid_lp   = np.empty(N_UNITS, dtype=np.uint16)
    uid_dl   = np.empty(N_UNITS, dtype=np.uint16)
    uid_pc   = np.empty(N_UNITS, dtype=np.uint8)
    uid_is1  = np.empty(N_UNITS, dtype=np.uint8)
    unit_raw = bytes(vin[unit_data_ds : unit_data_ds + N_UNITS * UNIT_SIZE])
    for i in range(N_UNITS):
        base = i * UNIT_SIZE
        uid_fidx[i] = struct.unpack_from('<H', unit_raw, base +  4)[0]
        uid_lp[i]   = struct.unpack_from('<H', unit_raw, base +  6)[0]
        uid_dl[i]   = struct.unpack_from('<H', unit_raw, base + 10)[0]
        uid_pc[i]   = unit_raw[base + 20]
        uid_is1[i]  = unit_raw[base + 21]
    print("  %d units parsed" % N_UNITS)

    units_by_fidx = defaultdict(list)
    for uid in range(N_UNITS):
        units_by_fidx[int(uid_fidx[uid])].append(uid)

    # -- Load Tom VIN --
    print("\nLoading tom.vin ...")
    tom_raw = bytearray(xor_decode(tom_vin_path))
    assert bytes(tom_raw[:4]) == b'RIFF' and bytes(tom_raw[8:12]) == b'svin'
    tom_chunks = {tag: (ds, sz) for tag, ds, sz in riff_chunks(tom_raw, start=12)}
    print("  %d bytes" % len(tom_raw))

    # ================================================================= #
    #  PATCH 1: hash -- Tom's original join cost hash                   #
    # ================================================================= #
    print("\n" + "=" * 60)
    print("PATCH 1/3: hash (Tom's original join costs)")
    print("=" * 60)

    hash_ds, hash_sz = vin_chunks[b'hash']
    tom_hash_ds, tom_hash_sz = tom_chunks[b'hash']
    tom_hash_body = bytes(tom_raw[tom_hash_ds : tom_hash_ds + tom_hash_sz])
    new_hash_chunk = pack_chunk('hash', tom_hash_body)
    print("  Using Tom's original hash (%d bytes)" % tom_hash_sz)
    print("  Hash chunk: %d -> %d bytes" % (hash_sz + 8, len(new_hash_chunk)))

    # ================================================================= #
    #  PATCH 2: prsl -- preselection candidates                         #
    # ================================================================= #
    print("\n" + "=" * 60)
    print("PATCH 2/3: prsl (preselection candidates)")
    print("=" * 60)

    prsl_ds, prsl_sz = vin_chunks[b'prsl']
    old_prsl_count = struct.unpack_from('<I', vin, prsl_ds)[0]
    print("  Old prsl: %d groups" % old_prsl_count)

    by_fidx_sorted = {}
    for fidx, uids in units_by_fidx.items():
        by_fidx_sorted[fidx] = sorted(uids, key=lambda uid: int(uid_lp[uid]))

    print("  Computing context keys ...")
    uid_context_key = np.full(N_UNITS, -1, dtype=np.int64)
    for fidx, uids in by_fidx_sorted.items():
        n = len(uids)
        for j, uid in enumerate(uids):
            pc  = int(uid_pc[uid])
            is1 = int(uid_is1[uid])
            left_hp = 0 if j == 0 else unit_hp(int(uid_pc[uids[j-1]]), int(uid_is1[uids[j-1]]))
            right_hp = HP_SILENCE if j == n - 1 else unit_hp(int(uid_pc[uids[j+1]]), int(uid_is1[uids[j+1]]))
            ck = compute_context_key(left_hp, pc, is1, right_hp)
            if ck is not None:
                uid_context_key[uid] = ck

    n_keyed = int((uid_context_key >= 0).sum())
    print("  %d units assigned a context_key" % n_keyed)

    print("  Computing per-unit run potential ...")
    uid_run_potential = np.zeros(N_UNITS, dtype=np.int32)
    for fidx, uids in by_fidx_sorted.items():
        n = len(uids)
        for j in range(n):
            uid = uids[j]
            if uid_dl[uid] <= 0:
                continue
            run_len = 1
            for k in range(j - 1, -1, -1):
                if uid_dl[uids[k]] > 0:
                    run_len += 1
                else:
                    break
            for k in range(j + 1, n):
                if uid_dl[uids[k]] > 0:
                    run_len += 1
                else:
                    break
            uid_run_potential[uid] = run_len

    n_preferred = int((uid_run_potential >= MIN_RUN_POTENTIAL).sum())
    n_short_run = int(((uid_run_potential > 0) & (uid_run_potential < MIN_RUN_POTENTIAL)).sum())
    print("  Preferred (run>=%d): %d  Short-run (<%d): %d" % (
        MIN_RUN_POTENTIAL, n_preferred, MIN_RUN_POTENTIAL, n_short_run))

    mara_ck = defaultdict(list)
    for uid in range(N_UNITS):
        ck = int(uid_context_key[uid])
        if ck >= 0 and uid_dl[uid] > 0:
            mara_ck[ck].append(uid)
    for ck in mara_ck:
        mara_ck[ck].sort(key=lambda uid: -uid_run_potential[uid])
    print("  %d distinct adjacency context_keys" % len(mara_ck))

    units_by_pc = defaultdict(list)
    for uid in range(N_UNITS):
        pc = int(uid_pc[uid])
        if pc < 46 and pc != 255 and uid_dl[uid] > 0:
            units_by_pc[pc].append(uid)
    rng = random.Random(42)
    for pc in units_by_pc:
        units_by_pc[pc].sort(key=lambda uid: -uid_run_potential[uid])
    print("  %d usable units across %d phones" % (
        sum(len(v) for v in units_by_pc.values()), len(units_by_pc)))

    _hp_to_pc = {}
    for pc in range(46):
        _hp_to_pc[HP_BASE[pc]]     = pc
        _hp_to_pc[HP_BASE[pc] + 1] = pc

    print("  Building back-off indexes ...")
    lc_index = defaultdict(list)
    cr_index = defaultdict(list)

    for ck, uids in mara_ck.items():
        left_hp   = ck // 10000
        center_hp = (ck // 100) % 100
        right_hp  = ck % 100
        lc_index[(left_hp, center_hp)].extend(uids)
        cr_index[(center_hp, right_hp)].extend(uids)

    for key in lc_index:
        lc_index[key] = list(set(lc_index[key]))
        lc_index[key].sort(key=lambda uid: -uid_run_potential[uid])
    for key in cr_index:
        cr_index[key] = list(set(cr_index[key]))
        cr_index[key].sort(key=lambda uid: -uid_run_potential[uid])
    print("  LC pairs: %d  CR pairs: %d" % (len(lc_index), len(cr_index)))

    # Load Tom's prsl
    print("  Loading Tom's prsl (keys + candidates) ...")
    tom_prsl_ds, tom_prsl_sz = tom_chunks[b'prsl']
    tom_prsl = {}
    tom_all_keys = set()
    pos = tom_prsl_ds
    n_tom_groups = struct.unpack_from('<I', tom_raw, pos)[0]
    pos += 4
    tom_total_cands = 0
    tom_empty_keys = 0
    for _ in range(n_tom_groups):
        grp_n  = struct.unpack_from('<I', tom_raw, pos)[0]
        grp_ck = struct.unpack_from('<I', tom_raw, pos + 4)[0]
        n_cands = grp_n - 1
        cands = list(struct.unpack_from('<%dI' % n_cands, tom_raw, pos + 8))
        pos += 4 + grp_n * 4
        tom_all_keys.add(grp_ck)
        valid = [uid for uid in cands if uid < N_UNITS and uid_dl[uid] > 0]
        valid.sort(key=lambda uid: -uid_run_potential[uid])
        if valid:
            tom_prsl[grp_ck] = valid
            tom_total_cands += len(valid)
        else:
            tom_empty_keys += 1
    print("  %d Tom groups, %d valid candidates kept" % (n_tom_groups, tom_total_cands))
    if tom_empty_keys:
        print("  %d Tom keys lost ALL candidates (dl=0) -- will back-fill" % tom_empty_keys)

    # Build target-recording index
    target_rec_by_pc = defaultdict(list)
    for uid in range(N_UNITS):
        fidx = int(uid_fidx[uid])
        if fidx in TARGET_RECORDING_FIDX and uid_dl[uid] > 0:
            pc = int(uid_pc[uid])
            if pc < 46:
                target_rec_by_pc[pc].append(uid)
    for pc in target_rec_by_pc:
        target_rec_by_pc[pc].sort(key=lambda uid: -uid_run_potential[uid])
    n_target_uids = sum(len(v) for v in target_rec_by_pc.values())
    print("  Target recording UIDs: %d across %d phones (from %d recordings)" % (
        n_target_uids, len(target_rec_by_pc), len(TARGET_RECORDING_FIDX)))

    all_target_keys = tom_all_keys | set(mara_ck.keys())
    print("  Target key space: %d keys" % len(all_target_keys))

    merged_ck = {}
    n_tom_only = 0
    n_mara_only = 0
    n_hybrid = 0
    empty = 0
    stats_source = [0, 0, 0, 0, 0, 0]

    for ck in all_target_keys:
        left_hp   = ck // 10000
        center_hp = (ck // 100) % 100
        right_hp  = ck % 100
        pc = _hp_to_pc.get(center_hp)
        if pc is None:
            continue

        cands = []
        existing = set()

        for uid in tom_prsl.get(ck, []):
            if uid not in existing:
                cands.append(uid)
                existing.add(uid)
        n_tom = len(cands)

        n_target = 0
        for uid in target_rec_by_pc.get(pc, []):
            if len(cands) >= MAX_CANDS_PER_GROUP:
                break
            if uid not in existing:
                cands.append(uid)
                existing.add(uid)
                n_target += 1

        for uid in mara_ck.get(ck, []):
            if len(cands) >= MAX_CANDS_PER_GROUP:
                break
            if uid not in existing:
                cands.append(uid)
                existing.add(uid)
        n_exact = len(cands) - n_tom - n_target

        for uid in lc_index.get((left_hp, center_hp), []):
            if len(cands) >= MAX_CANDS_PER_GROUP:
                break
            if uid not in existing:
                cands.append(uid)
                existing.add(uid)
        n_lc = len(cands) - n_tom - n_target - n_exact

        for uid in cr_index.get((center_hp, right_hp), []):
            if len(cands) >= MAX_CANDS_PER_GROUP:
                break
            if uid not in existing:
                cands.append(uid)
                existing.add(uid)
        n_cr = len(cands) - n_tom - n_target - n_exact - n_lc

        if len(cands) < MIN_BACKOFF_CANDS:
            ppool = units_by_pc.get(pc, [])
            for uid in ppool:
                if len(cands) >= MAX_CANDS_PER_GROUP:
                    break
                if uid not in existing:
                    cands.append(uid)
                    existing.add(uid)
        n_phone = len(cands) - n_tom - n_target - n_exact - n_lc - n_cr

        if cands:
            merged_ck[ck] = cands
            has_tom  = n_tom > 0
            has_mara = (n_exact + n_lc + n_cr + n_phone) > 0
            if has_tom and has_mara:
                n_hybrid += 1
            elif has_tom:
                n_tom_only += 1
            else:
                n_mara_only += 1
            stats_source[0] += n_tom
            stats_source[1] += n_target
            stats_source[2] += n_exact
            stats_source[3] += n_lc
            stats_source[4] += n_cr
            stats_source[5] += n_phone
        else:
            empty += 1

    print("  Populated: %d keys (%d hybrid, %d Tom-only, %d voice-only, %d empty/skipped)" % (
        len(merged_ck), n_hybrid, n_tom_only, n_mara_only, empty))
    print("  Candidate sources: tom_curated=%d  target_rec=%d  exact=%d  "
          "left+center=%d  center+right=%d  phone-fill=%d" % tuple(stats_source))

    sorted_cks = sorted(merged_ck.keys())
    parts = [struct.pack('<I', len(merged_ck))]
    for ck in sorted_cks:
        cands = merged_ck[ck]
        n = 1 + len(cands)
        parts.append(struct.pack('<I', n))
        parts.append(struct.pack('<I', ck))
        for uid in cands:
            parts.append(struct.pack('<I', uid))

    new_prsl_chunk = pack_chunk('prsl', b''.join(parts))
    print("  Prsl chunk: %d -> %d bytes" % (prsl_sz + 8, len(new_prsl_chunk)))

    # ================================================================= #
    #  PATCH 3: mean -- Tom's original per-halfphone statistics         #
    # ================================================================= #
    print("\n" + "=" * 60)
    print("PATCH 3/3: mean (Tom's original per-halfphone stats)")
    print("=" * 60)

    mean_ds, mean_sz = vin_chunks[b'mean']
    tom_mean_ds, tom_mean_sz = tom_chunks[b'mean']
    tom_mean_body = bytes(tom_raw[tom_mean_ds : tom_mean_ds + tom_mean_sz])
    new_mean_chunk = pack_chunk('mean', tom_mean_body)
    print("  Using Tom's original mean (%d bytes)" % tom_mean_sz)
    print("  Mean chunk: %d -> %d bytes" % (mean_sz + 8, len(new_mean_chunk)))

    # ================================================================= #
    #  Apply all three patches atomically                               #
    # ================================================================= #
    print("\n" + "=" * 60)
    print("Applying all patches to %s.vin ..." % voice_name)
    print("=" * 60)

    patches = [
        ('hash', hash_ds, hash_sz, new_hash_chunk),
        ('prsl', prsl_ds, prsl_sz, new_prsl_chunk),
        ('mean', mean_ds, mean_sz, new_mean_chunk),
    ]
    patches.sort(key=lambda x: -x[1])

    new_vin = bytes(vin)
    for name, ds, sz, chunk in patches:
        chunk_start = ds - 8
        chunk_end   = ds + sz + (sz & 1)
        new_vin = new_vin[:chunk_start] + chunk + new_vin[chunk_end:]
        print("  Patched %s" % name)

    new_riff_size = len(new_vin) - 8
    new_vin = new_vin[:4] + struct.pack('<I', new_riff_size) + new_vin[8:]

    encoded = bytes(np.frombuffer(new_vin, dtype=np.uint8) ^ XOR_KEY)
    tmp = vin_path + '.rest_tmp'
    with open(tmp, 'wb') as f:
        f.write(encoded)
    os.replace(tmp, vin_path)

    print("\n  Wrote %s  (%d bytes)" % (vin_path, len(encoded)))
    print("\nStep 3 done -- hash + prsl + mean rebuilt.")
    print("=" * 70)


# =========================================================================== #
#                                                                             #
#  STEP 4: Build Hash (spectral boundary distances)                           #
#                                                                             #
# =========================================================================== #
def step4_build_hash(voice_name, vin_path, vdb_path, tom_vin_path):
    """Patch join-cost values in the voice VIN hash chunk."""
    print("\n")
    print("=" * 70)
    print("STEP 4/5: Build hash (spectral boundary distances)")
    print("=" * 70)

    print("\nLoading %s.vin ..." % voice_name)
    vin = bytearray(xor_decode(vin_path))
    assert vin[:4] == b'RIFF' and vin[8:12] == b'svin'
    vin_chunks = {tag: (ds, sz) for tag, ds, sz in riff_chunks(vin, start=12)}

    cnts_ds, cnts_sz = vin_chunks[b'cnts']
    N_UNITS = struct.unpack_from('<I', vin, cnts_ds + 8)[0]

    unit_ds, unit_sz = vin_chunks[b'unit']
    unit_data_ds = None
    for tag, ds, sz in riff_chunks(vin, start=unit_ds, end=unit_ds+unit_sz):
        if tag == b'data':
            unit_data_ds = ds
            break
    assert unit_data_ds is not None

    uid_fidx = np.empty(N_UNITS, dtype=np.uint16)
    uid_lp   = np.empty(N_UNITS, dtype=np.uint16)
    uid_dl   = np.empty(N_UNITS, dtype=np.uint16)
    unit_raw = bytes(vin[unit_data_ds : unit_data_ds + N_UNITS * UNIT_SIZE])
    for i in range(N_UNITS):
        base = i * UNIT_SIZE
        uid_fidx[i] = struct.unpack_from('<H', unit_raw, base + 4)[0]
        uid_lp[i]   = struct.unpack_from('<H', unit_raw, base + 6)[0]
        uid_dl[i]   = struct.unpack_from('<H', unit_raw, base + 10)[0]
    print("  %d units parsed" % N_UNITS)

    # -- Compute per-unit run potential --
    print("  Computing run potential ...")
    uid_run_potential = np.zeros(N_UNITS, dtype=np.int32)
    units_by_fidx_hash = defaultdict(list)
    for uid in range(N_UNITS):
        if uid_dl[uid] > 0:
            units_by_fidx_hash[int(uid_fidx[uid])].append(uid)
    for fidx, uids in units_by_fidx_hash.items():
        uids_sorted = sorted(uids, key=lambda u: int(uid_lp[u]))
        n = len(uids_sorted)
        for j in range(n):
            run_len = 1
            for k in range(j - 1, -1, -1):
                if uid_dl[uids_sorted[k]] > 0:
                    run_len += 1
                else:
                    break
            for k in range(j + 1, n):
                if uid_dl[uids_sorted[k]] > 0:
                    run_len += 1
                else:
                    break
            uid_run_potential[uids_sorted[j]] = run_len
    n_highrun = int((uid_run_potential >= RUN_PENALTY_MIN).sum())
    print("  Run potential: %d units >= %d (high-run), %d < %d (penalized)" % (
        n_highrun, RUN_PENALTY_MIN,
        int(((uid_run_potential > 0) & (uid_run_potential < RUN_PENALTY_MIN)).sum()),
        RUN_PENALTY_MIN))

    # -- Parse feat -> filenames --
    feat_ds, feat_sz = vin_chunks[b'feat']
    feat = vin[feat_ds : feat_ds + feat_sz]
    fn_idx = feat.find(b'filename')
    fn_count = struct.unpack_from('<I', feat, fn_idx + 8)[0]
    p = fn_idx + 12
    filenames = {}
    for _ in range(fn_count):
        nlen = struct.unpack_from('<H', feat, p)[0]
        name = feat[p+2 : p+2+nlen].decode('latin-1', errors='replace')
        stored_id = struct.unpack_from('<I', feat, p+2+nlen)[0]
        filenames[stored_id] = name
        p += 2 + nlen + 4
    print("  %d filenames" % len(filenames))

    # -- Load VDB audio data --
    print("Loading %s8.vdb ..." % voice_name)
    vdb_raw = xor_decode(vdb_path)
    vdb_chunks_h = {tag: (ds, sz) for tag, ds, sz in riff_chunks(vdb_raw, start=12)}
    vdb_data_ds, vdb_data_sz = vdb_chunks_h[b'data']

    vdb_indx_ds, vdb_indx_sz = vdb_chunks_h[b'indx']
    vp = vdb_indx_ds
    vdb_count = struct.unpack_from('<I', vdb_raw, vp)[0]
    vp += 4
    vdb_entries = []
    for _ in range(vdb_count + 1):
        boff = struct.unpack_from('<I', vdb_raw, vp)[0]
        vp += 4
        nlen = struct.unpack_from('<H', vdb_raw, vp)[0]
        vp += 2
        name = vdb_raw[vp:vp+nlen].decode('ascii', errors='replace')
        vp += nlen
        vdb_entries.append((boff, name))

    vdb_rec_info = {}
    for i in range(len(vdb_entries) - 1):
        name = vdb_entries[i][1]
        if name:
            abs_off = vdb_data_ds + vdb_entries[i][0]
            nbytes = vdb_entries[i+1][0] - vdb_entries[i][0]
            vdb_rec_info[name] = (abs_off, nbytes)
    print("  %d VDB recordings" % len(vdb_rec_info))

    # -- Compute boundary spectral features --
    print("\nComputing boundary spectral features (%d-dim, %dms window) ..." % (
        N_FEATURES, BOUNDARY_MS))
    t0 = time.time()

    hann = np.hanning(BOUNDARY_SAMP).astype(np.float32)
    left_feat  = np.zeros((N_UNITS, N_FEATURES), dtype=np.float32)
    right_feat = np.zeros((N_UNITS, N_FEATURES), dtype=np.float32)
    feat_valid = np.zeros(N_UNITS, dtype=bool)

    units_by_fidx_s4 = defaultdict(list)
    for uid in range(N_UNITS):
        units_by_fidx_s4[int(uid_fidx[uid])].append(uid)

    n_computed = 0
    n_skipped = 0

    for fidx, uids in units_by_fidx_s4.items():
        rec_name = filenames.get(fidx, '')
        if rec_name not in vdb_rec_info:
            n_skipped += len(uids)
            continue
        abs_off, nbytes = vdb_rec_info[rec_name]

        for uid in uids:
            lp = int(uid_lp[uid])
            dl = int(uid_dl[uid])
            if dl < 2:
                n_skipped += 1
                continue

            bo_start = lp * 8
            bo_end = (lp + dl) * 8

            if bo_start + BOUNDARY_SAMP > nbytes or bo_end > nbytes:
                n_skipped += 1
                continue

            left_ulaw = vdb_raw[abs_off + bo_start : abs_off + bo_start + BOUNDARY_SAMP]
            right_start = max(bo_start, bo_end - BOUNDARY_SAMP)
            right_ulaw = vdb_raw[abs_off + right_start : abs_off + right_start + BOUNDARY_SAMP]

            if len(left_ulaw) < BOUNDARY_SAMP or len(right_ulaw) < BOUNDARY_SAMP:
                n_skipped += 1
                continue

            try:
                left_pcm = np.frombuffer(audioop.ulaw2lin(bytes(left_ulaw), 2), dtype='<i2').astype(np.float32)
                right_pcm = np.frombuffer(audioop.ulaw2lin(bytes(right_ulaw), 2), dtype='<i2').astype(np.float32)
            except Exception:
                n_skipped += 1
                continue

            left_fft = np.abs(np.fft.rfft(left_pcm * hann))
            right_fft = np.abs(np.fft.rfft(right_pcm * hann))

            left_log = np.log(left_fft[:N_FEATURES] + 1e-8)
            right_log = np.log(right_fft[:N_FEATURES] + 1e-8)

            left_feat[uid] = left_log
            right_feat[uid] = right_log
            feat_valid[uid] = True
            n_computed += 1

    elapsed = time.time() - t0
    print("  Computed: %d units (%.1fs)" % (n_computed, elapsed))
    print("  Skipped:  %d units (no audio / too short)" % n_skipped)

    # -- Per-recording spectral fingerprints --
    print("\nComputing per-recording spectral fingerprints ...")
    max_fidx = int(uid_fidx.max()) + 1
    rec_feat_sum = np.zeros((max_fidx, N_FEATURES), dtype=np.float64)
    rec_feat_cnt = np.zeros(max_fidx, dtype=np.int32)

    for uid in range(N_UNITS):
        if feat_valid[uid]:
            fi = int(uid_fidx[uid])
            rec_feat_sum[fi] += (left_feat[uid].astype(np.float64) +
                                 right_feat[uid].astype(np.float64)) * 0.5
            rec_feat_cnt[fi] += 1

    rec_has_feat = rec_feat_cnt > 0
    rec_mean = np.zeros((max_fidx, N_FEATURES), dtype=np.float32)
    rec_mean[rec_has_feat] = (rec_feat_sum[rec_has_feat] /
                              rec_feat_cnt[rec_has_feat, np.newaxis]).astype(np.float32)

    n_recs_with_feat = int(rec_has_feat.sum())
    print("  %d recordings have spectral fingerprints" % n_recs_with_feat)

    if CLUSTER_DISCOUNT < 1.0 and n_recs_with_feat > 10:
        valid_fidxs = np.where(rec_has_feat)[0]
        rng_np = np.random.RandomState(42)
        n_sample = min(5000, n_recs_with_feat * (n_recs_with_feat - 1) // 2)
        idx_a = rng_np.choice(valid_fidxs, size=n_sample)
        idx_b = rng_np.choice(valid_fidxs, size=n_sample)
        mask = idx_a != idx_b
        idx_a, idx_b = idx_a[mask], idx_b[mask]
        diffs = rec_mean[idx_a] - rec_mean[idx_b]
        sample_dists = np.sqrt(np.sum(diffs ** 2, axis=1))
        cluster_dist_thresh = float(np.percentile(sample_dists, CLUSTER_DIST_PCTILE))
        print("  Pairwise distance stats: mean=%.2f  std=%.2f  p25=%.2f  p50=%.2f  p75=%.2f" % (
            float(np.mean(sample_dists)), float(np.std(sample_dists)),
            float(np.percentile(sample_dists, 25)),
            float(np.percentile(sample_dists, 50)),
            float(np.percentile(sample_dists, 75))))
        print("  Cluster threshold (p%d): %.2f" % (CLUSTER_DIST_PCTILE, cluster_dist_thresh))
    else:
        cluster_dist_thresh = 0.0

    # -- Load Tom's hash, patch costs --
    print("\nLoading tom.vin hash structure ...")
    tom_raw_h = xor_decode(tom_vin_path)
    tom_chunks_h = {tag: (ds, sz) for tag, ds, sz in riff_chunks(tom_raw_h, start=12)}
    tom_hash_ds, tom_hash_sz = tom_chunks_h[b'hash']

    tom_hash_sub = {}
    for tag, ds, sz in riff_chunks(tom_raw_h, start=tom_hash_ds, end=tom_hash_ds+tom_hash_sz):
        tom_hash_sub[tag] = (ds, sz)

    tom_head_ds, _ = tom_hash_sub[b'head']
    n_rows  = struct.unpack_from('<I', tom_raw_h, tom_head_ds    )[0]
    n_cells = struct.unpack_from('<I', tom_raw_h, tom_head_ds + 4)[0]
    print("  n_rows=%d  n_cells=%d" % (n_rows, n_cells))

    tom_rows_ds, tom_rows_sz = tom_hash_sub[b'rows']
    rows = np.frombuffer(tom_raw_h[tom_rows_ds : tom_rows_ds + tom_rows_sz],
                         dtype=np.uint32).copy()

    tom_cell_ds, tom_cell_sz = tom_hash_sub[b'cell']
    cells_A = np.frombuffer(tom_raw_h[tom_cell_ds : tom_cell_ds + n_cells*4],
                            dtype=np.uint32).copy()
    cells_B = np.frombuffer(tom_raw_h[tom_cell_ds + n_cells*4 : tom_cell_ds + n_cells*8],
                            dtype=np.float32).copy()

    data_mask = cells_A != SENTINEL
    n_data = int(data_mask.sum())
    print("  %d data entries, %d sentinels" % (n_data, n_cells - n_data))

    tom_data_costs = cells_B[data_mask]
    print("  Tom cost stats: mean=%.2f std=%.2f max=%.2f" % (
        float(np.mean(tom_data_costs)), float(np.std(tom_data_costs)),
        float(np.max(tom_data_costs))))

    # -- Patch costs --
    print("\nComputing cost modifications on Tom's cells ...")
    t0 = time.time()

    nz_mask = rows > 0
    uid_right_vals = np.where(nz_mask)[0]
    start_vals = rows[uid_right_vals].astype(np.int64)

    sort_order = np.argsort(start_vals, kind='stable')
    sorted_starts = start_vals[sort_order]
    sorted_rights = uid_right_vals[sort_order]

    unique_starts, inverse = np.unique(sorted_starts, return_inverse=True)
    n_groups = len(unique_starts)
    print("  %d distinct offsets, %d uid_rights" % (n_groups, len(uid_right_vals)))

    group_fidx_sets = [set() for _ in range(n_groups)]
    group_rep_fidx = np.zeros(n_groups, dtype=np.int64)
    for i in range(len(sorted_rights)):
        ur = int(sorted_rights[i])
        gi = inverse[i]
        if ur < N_UNITS:
            fi = int(uid_fidx[ur])
            group_fidx_sets[gi].add(fi)
            if group_rep_fidx[gi] == 0 and rec_has_feat[fi]:
                group_rep_fidx[gi] = fi

    data_indices = np.where(data_mask)[0]
    gi_for_data = np.searchsorted(unique_starts, data_indices, side='right') - 1

    uid_left_all = cells_A[data_indices]
    valid_left = uid_left_all < N_UNITS
    fidx_left_all = np.zeros(len(data_indices), dtype=np.uint16)
    fidx_left_all[valid_left] = uid_fidx[uid_left_all[valid_left]]

    new_cells_B = cells_B.copy()

    sort_by_group = np.argsort(gi_for_data, kind='stable')
    sorted_gi = gi_for_data[sort_by_group]
    sorted_di = data_indices[sort_by_group]
    sorted_fl = fidx_left_all[sort_by_group]
    sorted_vl = valid_left[sort_by_group]

    group_boundaries = np.searchsorted(sorted_gi, np.arange(n_groups))
    group_boundaries = np.append(group_boundaries, len(sorted_gi))

    n_same_rec = 0
    n_similar_rec = 0
    n_tom_fallback = 0

    for gi in range(n_groups):
        lo = int(group_boundaries[gi])
        hi = int(group_boundaries[gi + 1])
        if lo >= hi:
            continue
        fidx_set = group_fidx_sets[gi]
        chunk_di = sorted_di[lo:hi]
        chunk_fl = sorted_fl[lo:hi]
        chunk_vl = sorted_vl[lo:hi]

        fidx_arr = np.array(list(fidx_set), dtype=np.uint16)
        same = chunk_vl & np.isin(chunk_fl, fidx_arr)
        new_cells_B[chunk_di[same]] = 0.0
        n_same_rec += int(same.sum())

        cross = ~same
        n_cross_rec = int(cross.sum())

        if CLUSTER_DISCOUNT < 1.0 and cluster_dist_thresh > 0:
            rep_fi = int(group_rep_fidx[gi])
            if rep_fi > 0 and rec_has_feat[rep_fi]:
                right_vec = rec_mean[rep_fi]
                cross_valid = cross & chunk_vl
                cross_indices = np.where(cross_valid)[0]
                for ci in cross_indices:
                    fl = int(chunk_fl[ci])
                    if rec_has_feat[fl]:
                        diff = rec_mean[fl] - right_vec
                        rec_dist = float(np.sqrt(np.dot(diff, diff)))
                        if rec_dist < cluster_dist_thresh:
                            orig = float(new_cells_B[chunk_di[ci]])
                            new_cells_B[chunk_di[ci]] = orig * CLUSTER_DISCOUNT
                            n_similar_rec += 1
                        else:
                            n_tom_fallback += 1
                    else:
                        n_tom_fallback += 1
                n_tom_fallback += n_cross_rec - int(cross_valid.sum())
            else:
                n_tom_fallback += n_cross_rec
        else:
            n_tom_fallback += n_cross_rec

        if gi % 20000 == 0 and gi > 0:
            print("    %d/%d groups (%.0f%%)" % (gi, n_groups, 100.0 * gi / n_groups))

    # Run-potential penalty
    n_penalized = 0
    if RUN_PENALTY_MULT > 1.0:
        print("  Applying run-potential penalty ...")
        max_ur = min(n_rows, N_UNITS)
        pen_mask = ((uid_run_potential[:max_ur] > 0) &
                    (uid_run_potential[:max_ur] < RUN_PENALTY_MIN) &
                    (rows[:max_ur] > 0))
        pen_uids = np.where(pen_mask)[0]
        print("    %d uid_rights to penalize" % len(pen_uids))

        for ur in pen_uids:
            rp = int(uid_run_potential[ur])
            penalty = 1.0 + (RUN_PENALTY_MULT - 1.0) * (RUN_PENALTY_MIN - rp) / RUN_PENALTY_MIN
            start = int(rows[ur])
            rest = cells_A[start:]
            sentinel_hits = np.where(rest == SENTINEL)[0]
            end = start + int(sentinel_hits[0]) if len(sentinel_hits) > 0 else n_cells
            region = new_cells_B[start:end]
            pos_mask = region > 0
            n_pos = int(pos_mask.sum())
            if n_pos > 0:
                region[pos_mask] = np.minimum(12.0, region[pos_mask] * penalty)
                n_penalized += n_pos

    elapsed = time.time() - t0
    print("  Done in %.1fs" % elapsed)
    print("  Same-recording:    %d (cost=0.0)" % n_same_rec)
    print("  Similar-recording: %d (cost=%.0f%% of Tom)" % (n_similar_rec, CLUSTER_DISCOUNT * 100))
    print("  Tom original cost: %d" % n_tom_fallback)
    print("  Run-penalized:     %d" % n_penalized)

    # -- Extend hash for extra units --
    extra_uids = [uid for uid in range(N_UNITS) if uid >= TOM_N_UNITS and uid_dl[uid] > 0]
    n_extra = len(extra_uids)

    new_rows = rows.copy()
    extra_offset = n_cells

    if n_extra > 0:
        print("\n  Adding %d extra units to hash (shared-offset extension) ..." % n_extra)

        extra_uid_lefts = set()
        for uid_right in extra_uids:
            fidx_r = int(uid_fidx[uid_right])
            for uid_left in units_by_fidx_hash.get(fidx_r, []):
                if uid_left != uid_right:
                    extra_uid_lefts.add(uid_left)

        max_extra_ul = max(extra_uid_lefts) if extra_uid_lefts else 0
        ext_size = max_extra_ul + 1

        ext_cells_A = np.full(ext_size, SENTINEL, dtype=np.uint32)
        ext_cells_B = np.zeros(ext_size, dtype=np.float32)

        for uid_left in extra_uid_lefts:
            ext_cells_A[uid_left] = uid_left
            ext_cells_B[uid_left] = 0.0

        for uid_right in extra_uids:
            if uid_right < n_rows:
                new_rows[uid_right] = extra_offset

        n_ext_data = int((ext_cells_A != SENTINEL).sum())
        print("  Extension: %d cells (%d data, %d empty)" % (
            ext_size, n_ext_data, ext_size - n_ext_data))
        print("  Shared offset: %d (all %d extra uid_rights)" % (extra_offset, n_extra))

        max_tom_rows = int(rows[:TOM_N_UNITS].max())
        max_access = max_tom_rows + max_extra_ul
        if max_access >= extra_offset + ext_size:
            extra_padding = max_access - (extra_offset + ext_size) + 1
            print("  Adding %d padding cells for OOB safety" % extra_padding)
            ext_cells_A = np.concatenate([ext_cells_A,
                np.full(extra_padding, SENTINEL, dtype=np.uint32)])
            ext_cells_B = np.concatenate([ext_cells_B,
                np.zeros(extra_padding, dtype=np.float32)])

        final_cells_A = np.concatenate([cells_A, ext_cells_A])
        final_cells_B = np.concatenate([new_cells_B, ext_cells_B])
        new_n_cells = len(final_cells_A)
    else:
        print("\nNo extra units.")
        final_cells_A = cells_A
        final_cells_B = new_cells_B
        new_n_cells = n_cells

    sentinel_mask = final_cells_A == SENTINEL
    final_cells_B[sentinel_mask] = MISS_PENALTY
    n_total_data = int((~sentinel_mask).sum())
    n_penalty = int(sentinel_mask.sum())
    print("  Final: n_cells=%d (%d data, %d penalty=%.1f) -- was %d" % (
        new_n_cells, n_total_data, n_penalty, MISS_PENALTY, n_cells))

    # -- Rebuild hash chunk --
    print("\nRebuilding hash chunk ...")

    head_body = struct.pack('<II', n_rows, new_n_cells)
    head_chunk = pack_chunk(b'head', head_body)
    rows_chunk = pack_chunk(b'rows', new_rows.tobytes())
    cell_body = final_cells_A.tobytes() + final_cells_B.tobytes()
    cell_chunk = pack_chunk(b'cell', cell_body)

    new_hash_payload = head_chunk + rows_chunk + cell_chunk
    new_hash_chunk = pack_chunk(b'hash', new_hash_payload)

    print("  Hash chunk: %d bytes (%.1f MB)" % (len(new_hash_chunk), len(new_hash_chunk) / 1e6))

    # -- Patch VIN --
    print("\nPatching %s.vin ..." % voice_name)

    mara_hash_ds, mara_hash_sz = vin_chunks[b'hash']
    hash_chunk_start = mara_hash_ds - 8
    hash_chunk_end   = mara_hash_ds + mara_hash_sz + (mara_hash_sz & 1)

    new_vin = bytes(vin[:hash_chunk_start]) + new_hash_chunk + bytes(vin[hash_chunk_end:])

    new_riff_size = len(new_vin) - 8
    new_vin = new_vin[:4] + struct.pack('<I', new_riff_size) + new_vin[8:]

    encoded = bytes(np.frombuffer(new_vin, dtype=np.uint8) ^ XOR_KEY)
    tmp_path = vin_path + '.hash_tmp'
    with open(tmp_path, 'wb') as f:
        f.write(encoded)
    os.replace(tmp_path, vin_path)

    print("  Wrote %s  (%d bytes, %.1f MB)" % (vin_path, len(encoded), len(encoded) / 1e6))
    print("\nStep 4 done -- hash rebuilt with perfect hash placement.")
    print("  Includes %d extra units with same-rec entries." % n_extra)
    print("=" * 70)


# =========================================================================== #
#                                                                             #
#  STEP 5: Build Trees (CART leaf patching)                                   #
#                                                                             #
# =========================================================================== #
def step5_build_trees(voice_name, vin_path, tom_vin_path):
    """Patch CART tree leaves for the voice."""
    print("\n")
    print("=" * 70)
    print("STEP 5/5: Build trees (CART leaf patching)")
    print("=" * 70)

    # Load both VINs
    print("\nLoading VINs ...")
    tom_vin = xor_decode(tom_vin_path)
    voice_vin = bytearray(xor_decode(vin_path))
    print("  Tom VIN:   %d bytes" % len(tom_vin))
    print("  Voice VIN: %d bytes" % len(voice_vin))

    # Load voice unit features for tree traversal
    print("\nLoading %s unit features ..." % voice_name)
    uf = load_unit_features(bytes(voice_vin))
    n_units = uf['n_units']
    print("  %d units loaded" % n_units)

    # Find f0tr and durt in Tom's VIN
    print("\nParsing Tom's CART trees ...")
    tom_f0tr_data = tom_durt_data = None
    for tag, ds, sz in riff_chunks(tom_vin, 12):
        if tag == b'f0tr':
            tom_f0tr_data = bytearray(tom_vin[ds:ds+sz])
        elif tag == b'durt':
            tom_durt_data = bytearray(tom_vin[ds:ds+sz])

    assert tom_f0tr_data is not None, "f0tr not found in Tom VIN"
    assert tom_durt_data is not None, "durt not found in Tom VIN"

    # -- f0tr: keep unchanged --
    f0_trees = find_tree_sub_chunks(bytes(tom_f0tr_data))
    f0_tree_bytes = bytes(tom_f0tr_data[f0_trees[0][0]:f0_trees[0][0]+f0_trees[0][1]])
    f0_nodes, _ = parse_tree_with_offsets(f0_tree_bytes)
    f0_leaves = [nd for nd in f0_nodes if nd['t'] == 'L']
    print("  f0tr: %d nodes (%d leaves) -- keeping unchanged" % (
        len(f0_nodes), len(f0_leaves)))
    print("    Leaf range: [%.2f .. %.2f] Hz" % (
        min(nd['mean'] for nd in f0_leaves),
        max(nd['mean'] for nd in f0_leaves)))

    # -- durt: parse trees and questions --
    d_trees = find_tree_sub_chunks(bytes(tom_durt_data))
    assert len(d_trees) == 47, "Expected 47 durt trees, got %d" % len(d_trees)
    durt_ques = find_ques_in_chunk(bytes(tom_durt_data))
    print("  durt: %d trees, %d questions" % (len(d_trees), len(durt_ques)))

    d_all_info = []
    for tds, tsz in d_trees:
        tree_bytes = bytes(tom_durt_data[tds:tds+tsz])
        nodes, lps = parse_tree_with_offsets(tree_bytes)
        d_all_info.append((tds, tsz, nodes, lps))

    # -- Route units through durt trees --
    print("\nRouting %d units through durt trees ..." % n_units)
    leaf_vals = defaultdict(lambda: defaultdict(list))

    for uid in range(n_units):
        pc = int(uf['pc'][uid])
        if pc >= 47:
            continue
        features = {
            1: int(uf['syl_type'][uid]),
            2: int(uf['syl_in_phrase'][uid]),
            3: int(uf['phone_left'][uid]),
            4: int(uf['phone_right'][uid]),
            5: int(uf['word_in_phrase'][uid]),
            8: int(uf['phone_in_syl'][uid]),
        }
        tree_nodes = d_all_info[pc][2]
        li = traverse_tree(tree_nodes, durt_ques, features)
        leaf_vals[pc][li].append(float(uf['f0_context'][uid]))

    # -- Compute per-leaf statistics and patch --
    if SKIP_DURT_RECOMPUTE:
        total_leaves = sum(
            len([nd for nd in info[2] if nd['t'] == 'L'])
            for info in d_all_info
        )
        print("\n  Keeping Tom's original durt leaf means (SKIP_DURT_RECOMPUTE=True)")
        print("  (DUR_WEIGHT=0 means these only affect WSOLA output duration, not unit selection)")
        print("  %d leaves unchanged across %d trees" % (total_leaves, len(d_all_info)))
        n_recomputed = 0
        n_kept = total_leaves
    else:
        print("\nRecomputing durt leaf values from %s's f0_context ..." % voice_name)
        n_recomputed = 0
        n_kept = 0

        for ti, (tds, tsz, nodes, lps) in enumerate(d_all_info):
            label = PHONE_LABELS_47[ti] if ti < len(PHONE_LABELS_47) else '?'
            leaves = [nd for nd in nodes if nd['t'] == 'L']
            if not leaves:
                continue

            old_means = [nd['mean'] for nd in leaves]
            phone_recomp = 0
            phone_kept = 0

            for nd in leaves:
                ni = nd['i']
                samples = leaf_vals[ti].get(ni, [])
                abs_off = tds + nd['mean_off']

                if len(samples) >= TREE_MIN_SAMPLES:
                    arr = np.array(samples)
                    new_mean = float(arr.mean())
                    struct.pack_into('<f', tom_durt_data, abs_off, new_mean)
                    phone_recomp += 1
                else:
                    phone_kept += 1

            n_recomputed += phone_recomp
            n_kept += phone_kept

            new_leaves_data = parse_tree_with_offsets(
                bytes(tom_durt_data[tds:tds+tsz]))[0]
            new_leaves_l = [nd for nd in new_leaves_data if nd['t'] == 'L']
            new_means = [nd['mean'] for nd in new_leaves_l]
            n_voice_units = sum(len(v) for v in leaf_vals[ti].values())
            print("  %2d %-4s: %d units, %d/%d leaves recomputed  "
                  "Tom [%.1f..%.1f] -> %s [%.1f..%.1f]" % (
                      ti, label, n_voice_units,
                      phone_recomp, phone_recomp + phone_kept,
                      min(old_means), max(old_means),
                      voice_name, min(new_means), max(new_means)))

    print("\n  Total: %d leaves recomputed, %d kept (Tom's original)" % (
        n_recomputed, n_kept))

    # Build new chunks
    new_f0tr_chunk = pack_chunk(b'f0tr', bytes(tom_f0tr_data))
    new_durt_chunk = pack_chunk(b'durt', bytes(tom_durt_data))

    # Find f0tr and durt chunk positions in voice VIN
    f0tr_ds = f0tr_sz = durt_ds = durt_sz = None
    for tag, ds, sz in riff_chunks(bytes(voice_vin), 12):
        if tag == b'f0tr':
            f0tr_ds, f0tr_sz = ds, sz
        elif tag == b'durt':
            durt_ds, durt_sz = ds, sz

    assert f0tr_ds is not None, "f0tr not found in voice VIN"
    assert durt_ds is not None, "durt not found in voice VIN"

    print("\nPatching %s.vin ..." % voice_name)
    print("  f0tr: offset=%d, size %d -> %d" % (
        f0tr_ds - 8, f0tr_sz, len(tom_f0tr_data)))
    print("  durt: offset=%d, size %d -> %d" % (
        durt_ds - 8, durt_sz, len(tom_durt_data)))

    patches = [
        ('f0tr', f0tr_ds, f0tr_sz, new_f0tr_chunk),
        ('durt', durt_ds, durt_sz, new_durt_chunk),
    ]
    patches.sort(key=lambda x: -x[1])

    result = bytes(voice_vin)
    for name, ds, sz, new_chunk in patches:
        chunk_start = ds - 8
        old_chunk_len = 8 + sz + (sz & 1)
        result = result[:chunk_start] + new_chunk + result[chunk_start + old_chunk_len:]
        print("  Patched %s at offset %d" % (name, chunk_start))

    new_riff_size = len(result) - 8
    result = result[:4] + struct.pack('<I', new_riff_size) + result[8:]

    print("\nWriting %s ..." % vin_path)
    with open(vin_path, 'wb') as f:
        f.write(xor_encode(result))
    print("  Wrote %d bytes (XOR-encoded)" % len(result))

    # Verify
    print("\nVerifying ...")
    check = xor_decode(vin_path)
    for tag, ds, sz in riff_chunks(check, 12):
        if tag == b'f0tr':
            trees = find_tree_sub_chunks(check[ds:ds+sz])
            nodes, _ = parse_tree_with_offsets(
                check[ds:ds+sz][trees[0][0]:trees[0][0]+trees[0][1]])
            leaves = [nd for nd in nodes if nd['t'] == 'L']
            print("  f0tr: %d nodes, leaf range [%.2f .. %.2f] Hz -- OK" % (
                len(nodes), min(nd['mean'] for nd in leaves),
                max(nd['mean'] for nd in leaves)))
        elif tag == b'durt':
            trees = find_tree_sub_chunks(check[ds:ds+sz])
            print("  durt: %d trees -- OK" % len(trees))

    print("\nStep 5 done.")
    print("=" * 70)


# =========================================================================== #
#  Main entry point                                                           #
# =========================================================================== #
def main():
    parser = argparse.ArgumentParser(
        description='Consolidated voice build pipeline for SpeechWorks Speechify 3.0.5.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Pipeline steps (run in order):
  1. build_voice   -- Build <voice>8.vdb + <voice>.vin from WAVs + MFA alignment
  2. build_extras  -- Replace low-quality units with extra recording data
  3. build_rest    -- Patch hash, prsl, and mean chunks (Tom's originals + voice candidates)
  4. build_hash    -- Patch join-cost values (spectral boundary distances)
  5. build_trees   -- Patch CART tree leaves (f0tr + durt)

Example:
  python build_voice_pipeline.py mara
  python build_voice_pipeline.py craig --skip-extras --workers 4
  python build_voice_pipeline.py mara --wav-dir resynth_rvc --tg-dir resynth_rvc
""")
    parser.add_argument('voice', help='Voice name (e.g., mara, craig)')
    parser.add_argument('--skip-extras', action='store_true',
                        help='Skip step 2 (extras dir may not exist)')
    parser.add_argument('--wav-dir', default=None,
                        help='WAV directory name under output/ (default: resynth_rvc)')
    parser.add_argument('--tg-dir', default=None,
                        help='TextGrid directory name under output/ (default: same as wav-dir)')
    parser.add_argument('--workers', type=int, default=os.cpu_count() or 4,
                        help='Thread count for parallel processing (default: cpu_count)')
    args = parser.parse_args()

    voice_name = args.voice.lower()
    n_workers = max(1, args.workers)

    _HERE = os.path.dirname(os.path.abspath(__file__))
    voice_dir = os.path.join(_HERE, 'en-US', voice_name)
    vin_path  = os.path.join(voice_dir, '%s.vin' % voice_name)
    vdb_path  = os.path.join(voice_dir, '%s8.vdb' % voice_name)
    tom_vin   = os.path.join(_HERE, 'en-US', 'tom', 'tom.vin')
    tom_vdb   = os.path.join(_HERE, 'en-US', 'tom', 'tom8.vdb')

    # Default WAV/TG dirs
    wav_dir_name = args.wav_dir if args.wav_dir else 'resynth_rvc'
    tg_dir_name  = args.tg_dir if args.tg_dir else wav_dir_name
    wav_dir = os.path.join(voice_dir, 'output', wav_dir_name)
    tg_dir  = os.path.join(voice_dir, 'output', tg_dir_name)

    # Banner
    print("=" * 70)
    print("  Voice Build Pipeline")
    print("  Voice:    %s" % voice_name)
    print("  VIN out:  %s" % vin_path)
    print("  VDB out:  %s" % vdb_path)
    print("  Tom VIN:  %s" % tom_vin)
    print("  Tom VDB:  %s" % tom_vdb)
    print("  WAV dir:  %s" % wav_dir)
    print("  TG dir:   %s" % tg_dir)
    print("  Workers:  %d" % n_workers)
    print("  Extras:   %s" % ("SKIP" if args.skip_extras else "enabled"))
    print("=" * 70)

    # Validate paths
    if not os.path.isfile(tom_vin):
        print("ERROR: Tom VIN not found: %s" % tom_vin)
        sys.exit(1)
    if not os.path.isfile(tom_vdb):
        print("ERROR: Tom VDB not found: %s" % tom_vdb)
        sys.exit(1)
    os.makedirs(voice_dir, exist_ok=True)

    # Kill Speechify before starting
    kill_speechify()

    # Run all steps
    t_total_start = time.time()

    step1_build_voice(voice_name, voice_dir, vin_path, vdb_path, tom_vin, tom_vdb,
                      wav_dir, tg_dir, n_workers)

    if not args.skip_extras:
        step2_build_extras(voice_name, voice_dir, vin_path, vdb_path, tom_vin, n_workers)
    else:
        print("\n")
        print("=" * 70)
        print("STEP 2/5: Build extras -- SKIPPED (--skip-extras)")
        print("=" * 70)

    step3_build_rest(voice_name, vin_path, tom_vin)

    step4_build_hash(voice_name, vin_path, vdb_path, tom_vin)

    step5_build_trees(voice_name, vin_path, tom_vin)

    t_total = time.time() - t_total_start
    print("\n")
    print("=" * 70)
    print("  ALL STEPS COMPLETE")
    print("  Voice: %s" % voice_name)
    print("  VIN:   %s" % vin_path)
    print("  VDB:   %s" % vdb_path)
    print("  Total time: %.1f seconds (%.1f minutes)" % (t_total, t_total / 60.0))
    print("=" * 70)
    print("\nRestart Speechify.exe and run synthesis test.")
    print("=" * 252)


if __name__ == '__main__':
    main()
