#!/usr/bin/env python3
"""
patch_durt.py -- Patch durt (duration) CART tree leaf means.

The durt chunk contains 47 per-phone CART trees that predict target duration
for each phonetic context. Tom's trees encode Tom's characteristic rhythm.
Modifying the leaf means changes which units the Viterbi prefers, biasing
selection toward units with different durations.

NOTE: durt trees do NOT rewrite output duration. They bias unit SELECTION.
Output timing = next_unit.lp - this_unit.lp (from VIN unit table).

Usage:
    python patch_durt.py --vin craig.vin --diag-only
    python patch_durt.py --vin craig.vin --output craig_durpatched.vin --scale 1.0 --expand 2.0
    python patch_durt.py --vin craig.vin --output craig_durpatched.vin --variance-scale 0.5

    --scale S           Multiply all leaf means by S (>1 = prefer longer units)
    --expand E          Expand range around per-phone median by factor E
    --variance-scale V  Multiply all leaf variances by V (<1 = tighter matching)
    --phone P           Only patch phone P (e.g. --phone aa --phone t)
    --diag-only         Show current leaf statistics per phone
"""

import argparse
import os
import sys
import struct
import numpy as np

XOR_KEY = 0xCE

PHONE_LABELS = [
    "aa", "ae", "ah", "ao", "aw", "ax", "ay", "b", "ch", "dx",
    "d", "dh", "eh", "el", "er", "en", "ey", "f", "g", "hh",
    "ih", "ix", "iy", "jh", "k", "l", "m", "n", "ng", "ow",
    "oy", "p", "pau", "r", "s", "sh", "t", "th", "uh", "uw",
    "v", "w", "xx", "y", "z", "zh", ""
]


def xor_decode(data):
    return bytes(b ^ XOR_KEY for b in data)


def riff_chunks(data, start=12):
    """Iterate top-level RIFF chunks."""
    pos = start
    while pos < len(data) - 8:
        cid = data[pos:pos+4].decode("ascii", errors="replace")
        csz = struct.unpack_from("<I", data, pos+4)[0]
        yield cid, pos, csz, data[pos+8:pos+8+csz]
        pos += 8 + csz + (csz % 2)


def parse_durt_trees(plain_vin):
    """Parse all durt trees from decoded VIN. Returns list of per-phone tree info."""
    # Find durt chunk
    durt_offset = None
    durt_data = None
    for cid, pos, csz, cdata in riff_chunks(plain_vin):
        if cid == "durt":
            durt_offset = pos
            durt_data = cdata
            break

    if durt_data is None:
        print("ERROR: no durt chunk found")
        return None

    # Parse sub-chunks within durt
    trees = []
    trhd_data = None
    p = 0
    while p < len(durt_data) - 8:
        sid = durt_data[p:p+4].decode("ascii", errors="replace")
        ssz = struct.unpack_from("<I", durt_data, p+4)[0]
        if sid == "trhd":
            trhd_data = durt_data[p+8:p+8+ssz]
        elif sid == "tree":
            tree_raw = durt_data[p+8:p+8+ssz]
            tree_abs_base = durt_offset + 8 + p + 8
            trees.append((tree_raw, tree_abs_base))
        p += 8 + ssz + (ssz % 2)

    if not trees:
        print("ERROR: no tree sub-chunks in durt")
        return None

    # Parse each tree
    all_phones = []
    for tree_idx, (tree_data, tree_abs_base) in enumerate(trees):
        n_nodes = struct.unpack_from("<I", tree_data, 0)[0]
        leaves = []
        pos = 4
        for i in range(n_nodes):
            if pos + 8 > len(tree_data):
                break
            node_index = struct.unpack_from("<I", tree_data, pos)[0]
            yes_child = struct.unpack_from("<i", tree_data, pos+4)[0]

            if yes_child < 0:  # Leaf (20 bytes)
                if pos + 20 > len(tree_data):
                    break
                mean = struct.unpack_from("<f", tree_data, pos+12)[0]
                variance = struct.unpack_from("<f", tree_data, pos+16)[0]
                mean_abs_offset = tree_abs_base + pos + 12
                var_abs_offset = tree_abs_base + pos + 16
                leaves.append({
                    "node": node_index,
                    "mean": mean,
                    "variance": variance,
                    "mean_offset": mean_abs_offset,
                    "var_offset": var_abs_offset,
                })
                pos += 20
            else:  # Branch (16 bytes)
                pos += 16

        phone = PHONE_LABELS[tree_idx] if tree_idx < len(PHONE_LABELS) else f"?{tree_idx}"
        all_phones.append({
            "phone": phone,
            "tree_idx": tree_idx,
            "n_nodes": n_nodes,
            "leaves": leaves,
        })

    return all_phones


def diag_report(phones):
    """Print diagnostic summary of all durt trees."""
    all_means = []
    all_vars = []

    print(f"\n  {'Phone':>5s}  {'Nodes':>5s}  {'Leaves':>6s}  "
          f"{'Mean min':>9s}  {'Mean med':>9s}  {'Mean max':>9s}  "
          f"{'Var min':>8s}  {'Var max':>8s}")
    print(f"  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*8}  {'-'*8}")

    for ph in phones:
        if not ph["leaves"]:
            print(f"  {ph['phone']:>5s}  {ph['n_nodes']:>5d}  {0:>6d}  {'(empty)':>9s}")
            continue
        means = np.array([l["mean"] for l in ph["leaves"]])
        varis = np.array([l["variance"] for l in ph["leaves"]])
        all_means.extend(means)
        all_vars.extend(varis)
        print(f"  {ph['phone']:>5s}  {ph['n_nodes']:>5d}  {len(ph['leaves']):>6d}  "
              f"{np.min(means):>9.2f}  {np.median(means):>9.2f}  {np.max(means):>9.2f}  "
              f"{np.min(varis):>8.4f}  {np.max(varis):>8.4f}")

    all_means = np.array(all_means)
    all_vars = np.array(all_vars)
    print(f"\n  Global stats ({len(all_means)} leaves across {len(phones)} phones):")
    print(f"    Duration means: min={np.min(all_means):.2f}  "
          f"median={np.median(all_means):.2f}  max={np.max(all_means):.2f}")
    print(f"    Duration units: 1 unit = 0.5ms (4 samples at 8kHz)")
    print(f"    Variances: min={np.min(all_vars):.4f}  "
          f"mean={np.mean(all_vars):.4f}  max={np.max(all_vars):.4f}")

    # Show top 10 longest and shortest phones by median mean
    phone_medians = []
    for ph in phones:
        if ph["leaves"]:
            med = np.median([l["mean"] for l in ph["leaves"]])
            phone_medians.append((ph["phone"], med))
    phone_medians.sort(key=lambda x: x[1], reverse=True)

    print(f"\n  Longest phones (by median leaf mean):")
    for name, med in phone_medians[:10]:
        print(f"    {name:>5s}: {med:7.2f} ({med*0.5:.1f} ms)")
    print(f"\n  Shortest phones (by median leaf mean):")
    for name, med in phone_medians[-10:]:
        print(f"    {name:>5s}: {med:7.2f} ({med*0.5:.1f} ms)")


def main():
    parser = argparse.ArgumentParser(
        description="Patch durt CART tree leaf values for prosody tuning")
    parser.add_argument("--vin", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Global scale for all leaf means (>1=longer, <1=shorter)")
    parser.add_argument("--expand", type=float, default=1.0,
                        help="Range expansion around per-phone median (>1=more variation)")
    parser.add_argument("--variance-scale", type=float, default=1.0,
                        help="Scale leaf variances (<1=tighter matching, >1=looser)")
    parser.add_argument("--phone", action="append", default=None,
                        help="Only patch specific phone(s). Repeat for multiple.")
    parser.add_argument("--diag-only", action="store_true",
                        help="Show current leaf statistics without patching")

    args = parser.parse_args()

    if args.output is None and not args.diag_only:
        base, ext = os.path.splitext(args.vin)
        args.output = f"{base}_durpatched{ext}"

    # Read and decode VIN
    with open(args.vin, "rb") as f:
        raw = f.read()
    plain = bytearray(xor_decode(raw))

    assert plain[:4] == b"RIFF", "Not a valid VIN (bad RIFF header after XOR decode)"
    assert plain[8:12] == b"svin", "Not a valid VIN (bad form ID)"

    # Parse trees
    phones = parse_durt_trees(plain)
    if phones is None:
        sys.exit(1)

    print(f"{'='*70}")
    print(f"  durt Tree Patcher")
    print(f"{'='*70}")
    print(f"  VIN: {args.vin}")
    print(f"  Trees: {len(phones)} (one per phone)")
    total_leaves = sum(len(ph["leaves"]) for ph in phones)
    print(f"  Total leaves: {total_leaves}")

    diag_report(phones)

    if args.diag_only:
        return

    # Determine which phones to patch
    target_phones = None
    if args.phone:
        target_phones = set(args.phone)
        unknown = target_phones - set(ph["phone"] for ph in phones)
        if unknown:
            print(f"\n  WARNING: unknown phone(s): {unknown}")
        print(f"\n  Targeting phones: {sorted(target_phones)}")

    no_change = (args.scale == 1.0 and args.expand == 1.0
                 and args.variance_scale == 1.0)
    if no_change:
        print("\n  No changes requested (scale=1.0, expand=1.0, variance_scale=1.0)")
        return

    # Apply patches
    patched_count = 0
    print(f"\n  Patching: scale={args.scale}, expand={args.expand}, "
          f"variance_scale={args.variance_scale}")

    for ph in phones:
        if not ph["leaves"]:
            continue
        if target_phones and ph["phone"] not in target_phones:
            continue

        means = np.array([l["mean"] for l in ph["leaves"]])
        variances = np.array([l["variance"] for l in ph["leaves"]])

        # Scale means
        new_means = means * args.scale

        # Expand around per-phone median
        if args.expand != 1.0 and len(new_means) > 1:
            median_val = np.median(new_means)
            if median_val > 0:
                # Work in log domain for proportional expansion
                log_ratio = np.log(new_means / median_val)
                log_expanded = log_ratio * args.expand
                new_means = median_val * np.exp(log_expanded)

        # Clamp means to valid range (must be positive)
        new_means = np.maximum(new_means, 1.0)

        # Scale variances
        new_vars = variances * args.variance_scale

        # Show per-phone changes
        if len(means) <= 5 or target_phones:
            for i, l in enumerate(ph["leaves"]):
                old_m = l["mean"]
                new_m = new_means[i]
                old_v = l["variance"]
                new_v = new_vars[i]
                delta_m = "" if abs(new_m - old_m) < 0.01 else f" ({new_m/old_m:.2f}x)"
                delta_v = "" if abs(new_v - old_v) < 0.0001 else f" ({new_v/old_v:.2f}x)"
                print(f"    {ph['phone']:>5s} leaf {l['node']:3d}: "
                      f"mean {old_m:7.2f} -> {new_m:7.2f}{delta_m}  "
                      f"var {old_v:.4f} -> {new_v:.4f}{delta_v}")
        else:
            print(f"    {ph['phone']:>5s}: {len(means)} leaves, "
                  f"mean {np.median(means):.2f} -> {np.median(new_means):.2f}, "
                  f"var {np.mean(variances):.4f} -> {np.mean(new_vars):.4f}")

        # Write patches
        for i, l in enumerate(ph["leaves"]):
            struct.pack_into("<f", plain, l["mean_offset"], float(new_means[i]))
            struct.pack_into("<f", plain, l["var_offset"], float(new_vars[i]))
            patched_count += 1

    print(f"\n  Patched {patched_count} leaves")

    # Verify a few
    print(f"  Verifying...")
    for ph in phones:
        if ph["leaves"] and (not target_phones or ph["phone"] in target_phones):
            l = ph["leaves"][0]
            val = struct.unpack_from("<f", plain, l["mean_offset"])[0]
            print(f"    {ph['phone']}: read back mean={val:.2f}")
            break

    # XOR encode and write
    encoded = xor_decode(bytes(plain))
    with open(args.output, "wb") as f:
        f.write(encoded)

    print(f"\n  Written: {args.output}")
    print(f"  Replace your VIN and test synthesis.")
    print(f"\n  Suggested experiments:")
    print(f"    --scale 0.9  --expand 1.0  (slightly faster overall)")
    print(f"    --scale 1.0  --expand 2.0  (more duration variation)")
    print(f"    --scale 1.0  --expand 0.5  (less variation, more uniform)")
    print(f"    --variance-scale 0.5       (tighter duration matching)")
    print(f"    --variance-scale 2.0       (looser, more candidate diversity)")
    print(f"    --phone pau --scale 1.5    (longer pauses only)")


if __name__ == "__main__":
    main()
