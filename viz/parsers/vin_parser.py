"""
Consolidated VIN file parser for the Speechify visualizer.
Handles XOR decoding, RIFF iteration, and parsing of all VIN chunks.
"""
import struct
import os

XOR_KEY = 0xCE

PHONE_LABELS = [
    "aa", "ae", "ah", "ao", "aw", "ax", "ay", "b", "ch", "dx",
    "d", "dh", "eh", "el", "er", "en", "ey", "f", "g", "hh",
    "ih", "ix", "iy", "jh", "k", "l", "m", "n", "ng", "ow",
    "oy", "p", "pau", "r", "s", "sh", "t", "th", "uh", "uw",
    "v", "w", "xx", "y", "z", "zh", ""
]

# Module-level cache
_cache = {}


def xor_decode(data):
    return bytes(b ^ XOR_KEY for b in data)


def riff_chunks(data, start=12, end=None):
    """Iterate RIFF chunks: yields (tag_str, data_offset, size)."""
    end = end or len(data)
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4].decode("ascii", errors="replace")
        sz = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def sub_chunks(data, start=0, end=None):
    """Iterate sub-chunks within a chunk's data region."""
    end = end or len(data)
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos+4].decode("ascii", errors="replace")
        sz = struct.unpack_from('<I', data, pos+4)[0]
        yield tag, pos+8, sz
        pos += 8 + sz + (sz & 1)


def load_vin(path):
    """Load and XOR-decode a VIN file. Cached."""
    path = os.path.abspath(path)
    if path not in _cache:
        with open(path, "rb") as f:
            raw = f.read()
        plain = xor_decode(raw)
        assert plain[:4] == b"RIFF", "Bad RIFF header"
        assert plain[8:12] == b"svin", "Not a VIN file"
        _cache[path] = plain
    return _cache[path]


def list_chunks(plain):
    """List all top-level RIFF chunks with tag, offset, size."""
    result = []
    for tag, data_off, sz in riff_chunks(plain):
        result.append({"tag": tag, "offset": data_off - 8, "data_offset": data_off, "size": sz})
    return result


def _find_chunk(plain, target_tag):
    """Find a chunk by tag, return (data_offset, size, data_bytes)."""
    for tag, off, sz in riff_chunks(plain):
        if tag == target_tag:
            return off, sz, plain[off:off+sz]
    return None, None, None


# -- LIST/INFO --
def parse_list_info(plain):
    """Parse LIST/INFO chunk for metadata fields."""
    _, _, data = _find_chunk(plain, "LIST")
    if data is None:
        return None
    list_type = data[:4].decode('ascii', errors='replace')
    fields = {}
    if list_type == 'INFO':
        for stag, soff, ssz in sub_chunks(data, start=4):
            sdata = data[soff:soff+ssz]
            fields[stag] = sdata.decode('latin-1', errors='replace').rstrip('\x00')
    return {"type": list_type, "fields": fields}


# -- vers --
def parse_vers(plain):
    _, _, data = _find_chunk(plain, "vers")
    if data is None:
        return None
    # vers contains version string as null-terminated ASCII
    return data.rstrip(b'\x00').decode('ascii', errors='replace')


# -- cnts --
def parse_cnts(plain):
    _, _, data = _find_chunk(plain, "cnts")
    if data is None:
        return None
    # cnts: at least 3 u32 values
    vals = struct.unpack_from('<III', data, 0)
    return {"val0": vals[0], "val1": vals[1], "n_units": vals[2]}


# -- feat (filenames) --
def parse_feat_filenames(plain):
    """Parse feat chunk to extract stored_id -> recording name mapping.
    Uses the proven approach from diag_ground_truth.py: search for 'filename'
    string within feat data, then read count + entries."""
    _, _, feat_data = _find_chunk(plain, "feat")
    if feat_data is None:
        return {}

    # Search for literal 'filename' string in feat data
    fn_idx = feat_data.find(b'filename')
    if fn_idx < 0:
        return {}

    fn_count = struct.unpack_from('<I', feat_data, fn_idx + 8)[0]
    pos = fn_idx + 12
    filenames = {}
    for _ in range(fn_count):
        if pos + 2 > len(feat_data):
            break
        nlen = struct.unpack_from('<H', feat_data, pos)[0]
        name = feat_data[pos+2:pos+2+nlen].decode('latin-1', errors='replace')
        stored_id = struct.unpack_from('<I', feat_data, pos+2+nlen)[0]
        filenames[stored_id] = name
        pos += 2 + nlen + 4

    return filenames


# -- unit table --
UNIT_RECORD_SIZE = 29

def parse_unit_table(plain):
    """Parse the full unit table. Returns (n_units, unit_data_bytes)."""
    _, _, chunk_data = _find_chunk(plain, "unit")
    if chunk_data is None:
        return 0, b''

    # unit chunk has sub-chunks; the actual data is in 'data' sub-chunk
    for stag, soff, ssz in sub_chunks(chunk_data):
        if stag == "data":
            data = chunk_data[soff:soff+ssz]
            n_units = len(data) // UNIT_RECORD_SIZE
            return n_units, data

    return 0, b''


def get_unit(unit_data, idx):
    """Extract a single unit record by index."""
    off = idx * UNIT_RECORD_SIZE
    if off + UNIT_RECORD_SIZE > len(unit_data):
        return None
    uid = struct.unpack_from('<I', unit_data, off)[0]
    file_idx = struct.unpack_from('<H', unit_data, off+4)[0]
    local_pos = struct.unpack_from('<H', unit_data, off+6)[0]
    prev_uid = struct.unpack_from('<H', unit_data, off+8)[0]
    dur_like = struct.unpack_from('<H', unit_data, off+10)[0]
    # Bytes 12-25: various features
    f0_start = struct.unpack_from('<H', unit_data, off+12)[0]
    f0_mid = struct.unpack_from('<H', unit_data, off+14)[0]
    f0_end = struct.unpack_from('<H', unit_data, off+16)[0]
    syl_type = unit_data[off+18]
    syl_in_phrase = unit_data[off+19]
    pc = unit_data[off+20]
    is_first = unit_data[off+21]
    phone_code = unit_data[off+26]
    half = unit_data[off+27]

    phone = PHONE_LABELS[phone_code] if phone_code < len(PHONE_LABELS) else f"?{phone_code}"
    return {
        "uid": uid,
        "idx": idx,
        "file_idx": file_idx,
        "local_pos": local_pos,
        "prev_uid": prev_uid,
        "dur_like": dur_like,
        "f0_start": f0_start,
        "f0_mid": f0_mid,
        "f0_end": f0_end,
        "syl_type": syl_type,
        "syl_in_phrase": syl_in_phrase,
        "pc": pc,
        "is_first": is_first,
        "phone_code": phone_code,
        "phone": phone,
        "half": half,
    }


def get_units_page(unit_data, page=0, per_page=100, phone_filter=None, fidx_filter=None):
    """Get a page of units with optional filtering."""
    n_units = len(unit_data) // UNIT_RECORD_SIZE

    # Build filtered index if filters active
    if phone_filter is not None or fidx_filter is not None:
        filtered = []
        for i in range(n_units):
            off = i * UNIT_RECORD_SIZE
            if phone_filter is not None:
                pc = unit_data[off+26]
                ph = PHONE_LABELS[pc] if pc < len(PHONE_LABELS) else ""
                if ph != phone_filter:
                    continue
            if fidx_filter is not None:
                fidx = struct.unpack_from('<H', unit_data, off+4)[0]
                if fidx != fidx_filter:
                    continue
            filtered.append(i)
        total = len(filtered)
        start = page * per_page
        page_indices = filtered[start:start+per_page]
    else:
        total = n_units
        start = page * per_page
        page_indices = range(start, min(start + per_page, n_units))

    items = [get_unit(unit_data, i) for i in page_indices]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


# -- f0tr tree --
def parse_f0tr_tree(plain):
    """Parse f0tr CART tree. Returns dict with labels, questions, nodes."""
    _, f0tr_off, f0tr_data = _find_chunk(plain, "f0tr")
    if f0tr_data is None:
        return None

    result = {"labels": [], "questions": [], "nodes": []}

    # Parse sub-chunks: trhd (contains labl + ques), tree
    for stag, soff, ssz in sub_chunks(f0tr_data):
        sdata = f0tr_data[soff:soff+ssz]
        if stag == "trhd":
            # trhd contains labl and ques sub-chunks
            for stag2, soff2, ssz2 in sub_chunks(sdata):
                sdata2 = sdata[soff2:soff2+ssz2]
                if stag2 == "labl":
                    # Null-separated label strings
                    result["labels"] = [s for s in sdata2.decode('ascii', errors='replace').split('\x00') if s]
                elif stag2 == "ques":
                    # Question definitions
                    result["questions"] = [s for s in sdata2.decode('ascii', errors='replace').split('\x00') if s]

        elif stag == "tree":
            # Parse tree nodes
            n_nodes = struct.unpack_from('<I', sdata, 0)[0]
            pos = 4
            for i in range(n_nodes):
                if pos + 8 > len(sdata):
                    break
                node_index = struct.unpack_from('<I', sdata, pos)[0]
                yes_child = struct.unpack_from('<i', sdata, pos+4)[0]

                if yes_child < 0:  # Leaf (20 bytes)
                    if pos + 20 > len(sdata):
                        break
                    mean = struct.unpack_from('<f', sdata, pos+12)[0]
                    variance = struct.unpack_from('<f', sdata, pos+16)[0]
                    result["nodes"].append({
                        "node_index": node_index,
                        "is_leaf": True,
                        "yes_child": yes_child,
                        "mean": round(mean, 4),
                        "variance": round(variance, 6),
                    })
                    pos += 20
                else:  # Branch (16 bytes)
                    if pos + 16 > len(sdata):
                        break
                    no_child = struct.unpack_from('<i', sdata, pos+8)[0]
                    question_idx = struct.unpack_from('<I', sdata, pos+12)[0]
                    result["nodes"].append({
                        "node_index": node_index,
                        "is_leaf": False,
                        "yes_child": yes_child,
                        "no_child": no_child,
                        "question_idx": question_idx,
                    })
                    pos += 16

    return result


# -- durt trees --
def parse_durt_trees(plain):
    """Parse all 47 per-phone duration CART trees."""
    _, durt_off, durt_data = _find_chunk(plain, "durt")
    if durt_data is None:
        return None

    trees = []
    trhd_labels = []
    trhd_questions = []

    for stag, soff, ssz in sub_chunks(durt_data):
        sdata = durt_data[soff:soff+ssz]
        if stag == "trhd":
            for stag2, soff2, ssz2 in sub_chunks(sdata):
                sdata2 = sdata[soff2:soff2+ssz2]
                if stag2 == "labl":
                    trhd_labels = [s for s in sdata2.decode('ascii', errors='replace').split('\x00') if s]
                elif stag2 == "ques":
                    trhd_questions = [s for s in sdata2.decode('ascii', errors='replace').split('\x00') if s]

        elif stag == "tree":
            n_nodes = struct.unpack_from('<I', sdata, 0)[0]
            nodes = []
            pos = 4
            for i in range(n_nodes):
                if pos + 8 > len(sdata):
                    break
                node_index = struct.unpack_from('<I', sdata, pos)[0]
                yes_child = struct.unpack_from('<i', sdata, pos+4)[0]
                if yes_child < 0:
                    if pos + 20 > len(sdata):
                        break
                    mean = struct.unpack_from('<f', sdata, pos+12)[0]
                    variance = struct.unpack_from('<f', sdata, pos+16)[0]
                    nodes.append({
                        "node_index": node_index,
                        "is_leaf": True,
                        "yes_child": yes_child,
                        "mean": round(mean, 4),
                        "variance": round(variance, 6),
                    })
                    pos += 20
                else:
                    if pos + 16 > len(sdata):
                        break
                    no_child = struct.unpack_from('<i', sdata, pos+8)[0]
                    question_idx = struct.unpack_from('<I', sdata, pos+12)[0]
                    nodes.append({
                        "node_index": node_index,
                        "is_leaf": False,
                        "yes_child": yes_child,
                        "no_child": no_child,
                        "question_idx": question_idx,
                    })
                    pos += 16

            phone_idx = len(trees)
            phone = PHONE_LABELS[phone_idx] if phone_idx < len(PHONE_LABELS) else f"?{phone_idx}"
            trees.append({
                "phone": phone,
                "tree_idx": phone_idx,
                "n_nodes": n_nodes,
                "nodes": nodes,
            })

    return {
        "labels": trhd_labels,
        "questions": trhd_questions,
        "trees": trees,
    }


# -- hash --
def parse_hash_stats(plain):
    """Parse hash chunk header for statistics."""
    _, _, hash_data = _find_chunk(plain, "hash")
    if hash_data is None:
        return None

    # hash has sub-chunks: cell (float array) and rows (u32 array)
    cell_count = 0
    rows_count = 0
    for stag, soff, ssz in sub_chunks(hash_data):
        if stag == "cell":
            cell_count = ssz // 4  # float32 values
        elif stag == "rows":
            rows_count = ssz // 4  # u32 values

    return {"n_cells": cell_count, "n_rows": rows_count}


# -- mean --
def parse_mean(plain):
    """Parse mean chunk: per-phone feature means for Z-score normalization.
    Format: u32 n_phones, u32 n_features, f32[n_phones][n_features]."""
    _, _, data = _find_chunk(plain, "mean")
    if data is None:
        return None
    n_phones = struct.unpack_from('<I', data, 0)[0]
    n_features = struct.unpack_from('<I', data, 4)[0]

    FEATURE_NAMES = ["duration", "dur_z", "pitch", "pitch_z",
                     "voice", "voice_z", "power", "power_z"]

    rows = []
    for p in range(n_phones):
        row = {}
        for f in range(n_features):
            val = struct.unpack_from('<f', data, 8 + (p * n_features + f) * 4)[0]
            fname = FEATURE_NAMES[f] if f < len(FEATURE_NAMES) else f"f{f}"
            row[fname] = round(val, 4)
        # Phone label: index maps to halfphone (phone_idx = p // 2, half = p % 2)
        phone_idx = p // 2
        half = p % 2
        phone = PHONE_LABELS[phone_idx] if phone_idx < len(PHONE_LABELS) else f"?{phone_idx}"
        row["phone"] = phone
        row["half"] = half
        row["index"] = p
        rows.append(row)

    return {"n_phones": n_phones, "n_features": n_features,
            "features": FEATURE_NAMES[:n_features], "rows": rows}


# -- hist --
def parse_hist(plain):
    """Parse hist chunk: Z-score histogram for target cost lookup.
    Sub-chunks: head (n_bins, range_start), data (f32 per bin)."""
    _, _, hist_data = _find_chunk(plain, "hist")
    if hist_data is None:
        return None

    head_data = None
    bin_data = None
    for stag, soff, ssz in sub_chunks(hist_data):
        sdata = hist_data[soff:soff+ssz]
        if stag == "head":
            head_data = sdata
        elif stag == "data":
            bin_data = sdata

    if head_data is None or bin_data is None:
        return None

    n_bins = struct.unpack_from('<I', head_data, 0)[0]
    range_start = struct.unpack_from('<i', head_data, 4)[0]

    bins = []
    for i in range(n_bins):
        val = struct.unpack_from('<f', bin_data, i * 4)[0]
        z_score = range_start + i
        bins.append({"bin": i, "z_score": z_score, "neg_log_p": round(val, 4)})

    return {"n_bins": n_bins, "range_start": range_start, "bins": bins}


# -- prsl --
def parse_prsl_stats(plain):
    """Parse prsl chunk header for statistics."""
    _, _, data = _find_chunk(plain, "prsl")
    if data is None:
        return None

    count = struct.unpack_from('<I', data, 0)[0]
    # Scan groups for stats
    pos = 4
    total_candidates = 0
    min_cands = 999999
    max_cands = 0
    for _ in range(count):
        if pos + 4 > len(data):
            break
        n = struct.unpack_from('<I', data, pos)[0]
        n_cands = n - 1  # first entry is context_key
        total_candidates += n_cands
        min_cands = min(min_cands, n_cands)
        max_cands = max(max_cands, n_cands)
        pos += 4 + n * 4

    avg_cands = total_candidates / count if count > 0 else 0
    return {
        "n_groups": count,
        "total_candidates": total_candidates,
        "avg_candidates": round(avg_cands, 1),
        "min_candidates": min_cands,
        "max_candidates": max_cands,
    }


def prsl_lookup(plain, context_key):
    """Look up preselection candidates for a context key."""
    _, _, data = _find_chunk(plain, "prsl")
    if data is None:
        return None

    count = struct.unpack_from('<I', data, 0)[0]
    pos = 4
    for _ in range(count):
        if pos + 4 > len(data):
            break
        n = struct.unpack_from('<I', data, pos)[0]
        key = struct.unpack_from('<I', data, pos + 4)[0]
        if key == context_key:
            candidates = []
            for j in range(1, n):
                candidates.append(struct.unpack_from('<I', data, pos + 4 + j * 4)[0])
            return candidates
        if key > context_key:
            break  # sorted, won't find it
        pos += 4 + n * 4

    return None


# -- ccos --
def parse_ccos_summary(plain):
    """Parse ccos chunk for summary info."""
    _, _, ccos_data = _find_chunk(plain, "ccos")
    if ccos_data is None:
        return None

    labels = []
    data_size = 0
    for stag, soff, ssz in sub_chunks(ccos_data):
        sdata = ccos_data[soff:soff+ssz]
        if stag == "labl":
            n_labels = struct.unpack_from('<I', sdata, 0)[0]
            pos = 4
            for _ in range(n_labels):
                if pos + 2 > len(sdata):
                    break
                nlen = struct.unpack_from('<H', sdata, pos)[0]
                name = sdata[pos+2:pos+2+nlen].decode('ascii', errors='replace').rstrip('\x00')
                labels.append(name)
                pos += 2 + nlen
        elif stag == "data":
            data_size = ssz

    n_phones = len(labels)
    entries_per_phone = 722
    floats_per_entry = 12
    expected_size = n_phones * entries_per_phone * floats_per_entry * 4

    return {
        "n_phones": n_phones,
        "labels": labels,
        "entries_per_phone": entries_per_phone,
        "floats_per_entry": floats_per_entry,
        "data_size": data_size,
        "expected_size": expected_size,
        "description": "Boundary spectral feature vectors (12-dim MFCC-like) per phone halfphone, used for join cost on hash misses.",
    }


# -- ckls/cklx --
def parse_cklx_full(plain):
    """Parse full cklx inverted index with all entries."""
    _, _, cklx = _find_chunk(plain, "cklx")
    if cklx is None:
        return None

    group_count = struct.unpack_from('<I', cklx, 0)[0]
    pos = 4
    groups = []
    for _ in range(group_count):
        if pos + 2 > len(cklx):
            break
        nlen = struct.unpack_from('<H', cklx, pos)[0]
        name = cklx[pos+2:pos+2+nlen].decode('ascii', errors='replace').rstrip('\x00')
        pos += 2 + nlen
        entry_count = struct.unpack_from('<I', cklx, pos)[0]
        pos += 4

        entries = []
        for _ in range(entry_count):
            if pos + 2 > len(cklx):
                break
            klen = struct.unpack_from('<H', cklx, pos)[0]
            key = cklx[pos+2:pos+2+klen].decode('latin-1', errors='replace').rstrip('\x00')
            pos += 2 + klen
            pcount = struct.unpack_from('<I', cklx, pos)[0]
            pos += 4
            postings = []
            for _ in range(pcount):
                postings.append(struct.unpack_from('<I', cklx, pos)[0])
                pos += 4
            entries.append({"key": key, "posting_count": pcount})

        groups.append({
            "name": name,
            "entry_count": entry_count,
            "entries": entries,
        })

    return {"group_count": group_count, "groups": groups}


# -- ckls (full parse) --
def parse_ckls(plain):
    """Parse ckls token occurrence streams.
    Returns dict mapping recording name -> {words: [...], syllables: [...]}
    Each entry: {text, span_start, span_end}."""
    _, _, ckls_data = _find_chunk(plain, "ckls")
    if ckls_data is None:
        return None

    group_count = struct.unpack_from('<I', ckls_data, 0)[0]
    pos = 4

    # Result: rec_name -> {words: [], syllables: []}
    rec_tokens = {}

    for gi in range(group_count):
        if pos + 2 > len(ckls_data):
            break
        nlen = struct.unpack_from('<H', ckls_data, pos)[0]
        group_name = ckls_data[pos+2:pos+2+nlen].decode('ascii', errors='replace').rstrip('\x00')
        pos += 2 + nlen
        token_count = struct.unpack_from('<I', ckls_data, pos)[0]
        _unk = struct.unpack_from('<I', ckls_data, pos+4)[0]
        pos += 8

        token_type = 'words' if 'WORD' in group_name else 'syllables'

        # Alternating: token record, filename record, token record, ...
        for ti in range(token_count):
            # Token record
            if pos + 2 > len(ckls_data):
                break
            tlen = struct.unpack_from('<H', ckls_data, pos)[0]
            token_text = ckls_data[pos+2:pos+2+tlen].decode('latin-1', errors='replace').rstrip('\x00')
            pos += 2 + tlen
            if pos + 8 > len(ckls_data):
                break
            span_start = struct.unpack_from('<I', ckls_data, pos)[0]
            span_end = struct.unpack_from('<I', ckls_data, pos+4)[0]
            pos += 8

            # Filename record
            if pos + 2 > len(ckls_data):
                break
            flen = struct.unpack_from('<H', ckls_data, pos)[0]
            filename = ckls_data[pos+2:pos+2+flen].decode('latin-1', errors='replace').rstrip('\x00')
            pos += 2 + flen
            # file_id follows (u32) except possibly the last record
            if ti < token_count - 1:
                if pos + 4 <= len(ckls_data):
                    pos += 4  # skip file_id

            # Store
            if filename not in rec_tokens:
                rec_tokens[filename] = {'words': [], 'syllables': []}
            rec_tokens[filename][token_type].append({
                'text': token_text,
                'span_start': span_start,
                'span_end': span_end,
            })

    return rec_tokens


def search_words(plain, query):
    """Search cklx word index for a query string. Returns matching words
    with their recording names (via ckls span -> unit table -> file_idx -> feat filename)."""
    _, _, cklx = _find_chunk(plain, "cklx")
    if cklx is None:
        return []

    query_lower = query.lower()
    group_count = struct.unpack_from('<I', cklx, 0)[0]
    pos = 4

    matches = []
    for gi in range(group_count):
        if pos + 2 > len(cklx):
            break
        nlen = struct.unpack_from('<H', cklx, pos)[0]
        group_name = cklx[pos+2:pos+2+nlen].decode('ascii', errors='replace').rstrip('\x00')
        pos += 2 + nlen
        entry_count = struct.unpack_from('<I', cklx, pos)[0]
        pos += 4

        for _ in range(entry_count):
            if pos + 2 > len(cklx):
                break
            klen = struct.unpack_from('<H', cklx, pos)[0]
            key = cklx[pos+2:pos+2+klen].decode('latin-1', errors='replace').rstrip('\x00')
            pos += 2 + klen
            pcount = struct.unpack_from('<I', cklx, pos)[0]
            pos += 4
            postings = []
            for _ in range(pcount):
                postings.append(struct.unpack_from('<I', cklx, pos)[0])
                pos += 4

            if query_lower in key.lower():
                matches.append({
                    'word': key,
                    'group': group_name,
                    'posting_count': pcount,
                    'postings': postings,
                })

    return matches


def resolve_word_recordings(plain, word_matches, unit_data, filenames):
    """Resolve word search matches to recording names using ckls spans.
    Each posting ID in cklx is an index into the ckls token stream,
    and the span_start unit tells us the file_idx -> recording name."""
    # Parse ckls to get spans for each posting
    ckls_tokens = _parse_ckls_tokens(plain)
    if not ckls_tokens:
        return word_matches

    for match in word_matches:
        recs = set()
        for pid in match['postings']:
            group_key = match['group']
            if group_key in ckls_tokens and pid < len(ckls_tokens[group_key]):
                tok = ckls_tokens[group_key][pid]
                # Look up file_idx from the unit at span_start
                span_start = tok.get('span_start', -1)
                if 0 <= span_start < len(unit_data) // UNIT_RECORD_SIZE:
                    fidx = struct.unpack_from('<H', unit_data, span_start * UNIT_RECORD_SIZE + 4)[0]
                    rec_name = filenames.get(fidx, f'?{fidx}')
                    recs.add(rec_name)
        match['recordings'] = sorted(recs)
    return word_matches


def _parse_ckls_tokens(plain):
    """Parse ckls into ordered token lists per group for posting ID lookup."""
    _, _, ckls_data = _find_chunk(plain, "ckls")
    if ckls_data is None:
        return None

    group_count = struct.unpack_from('<I', ckls_data, 0)[0]
    pos = 4
    result = {}

    for gi in range(group_count):
        if pos + 2 > len(ckls_data):
            break
        nlen = struct.unpack_from('<H', ckls_data, pos)[0]
        group_name = ckls_data[pos+2:pos+2+nlen].decode('ascii', errors='replace').rstrip('\x00')
        pos += 2 + nlen
        token_count = struct.unpack_from('<I', ckls_data, pos)[0]
        _unk = struct.unpack_from('<I', ckls_data, pos+4)[0]
        pos += 8

        tokens = []
        for ti in range(token_count):
            if pos + 2 > len(ckls_data):
                break
            tlen = struct.unpack_from('<H', ckls_data, pos)[0]
            text = ckls_data[pos+2:pos+2+tlen].decode('latin-1', errors='replace').rstrip('\x00')
            pos += 2 + tlen
            if pos + 8 > len(ckls_data):
                break
            span_start = struct.unpack_from('<I', ckls_data, pos)[0]
            span_end = struct.unpack_from('<I', ckls_data, pos+4)[0]
            pos += 8
            tokens.append({'text': text, 'span_start': span_start, 'span_end': span_end})

            # Skip filename record
            if pos + 2 > len(ckls_data):
                break
            flen = struct.unpack_from('<H', ckls_data, pos)[0]
            pos += 2 + flen
            if ti < token_count - 1:
                if pos + 4 <= len(ckls_data):
                    pos += 4

        result[group_name] = tokens

    return result


def hash_lookup(plain, uid_left, uid_right):
    """Look up join cost between two units using the compressed perfect hash."""
    _, _, hash_data = _find_chunk(plain, "hash")
    if hash_data is None:
        return None

    cell_data = None
    rows_data = None
    for stag, soff, ssz in sub_chunks(hash_data):
        sdata = hash_data[soff:soff+ssz]
        if stag == "cell":
            cell_data = sdata
        elif stag == "rows":
            rows_data = sdata

    if cell_data is None or rows_data is None:
        return None

    n_rows = len(rows_data) // 4
    n_cells = len(cell_data) // 4

    if uid_right >= n_rows:
        return None

    row_offset = struct.unpack_from('<I', rows_data, uid_right * 4)[0]
    cell_idx = row_offset + uid_left

    if cell_idx >= n_cells:
        return None

    cost = struct.unpack_from('<f', cell_data, cell_idx * 4)[0]
    return round(cost, 6)
