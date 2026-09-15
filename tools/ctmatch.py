#!/usr/bin/env python3
"""
ctmatch - identify CT-485 payload fields automatically by correlating a bus
capture against Home Assistant `daikinone` diagnostics exports.

This is the tool that closes the loop. Instead of transcribing numbers off a
thermostat screen and hunting for them one at a time with `ctcap find`, you
take diagnostics exports alongside a capture and let every live cloud field
search the capture at once.

    python tools/ctmatch.py captures/run.log \\
        --fields <ha-daikinone>/custom_components/daikinone/client/fields.py \\
        --export exports/a.json@18:22:31 \\
        --export exports/b.json@18:31:04 \\
        --export exports/c.json@18:44:50 \\
        --mdi

Give it three or more exports taken at genuinely different operating points.
A slot that matches one export is usually a coincidence; a slot that tracks
three *distinct* values is almost certainly the field.

Scoring reflects that: a candidate is ranked by how many distinct target values
it correctly predicted, not by how many times it matched.

Timestamps: `path@HH:MM:SS` sets the moment an export was taken, matched
against the capture's own log timestamps. Without `@`, the file's modification
time is used, which is usually close enough if you exported and saved directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_cloud_fields import DEFAULT_DEVICE, SENTINEL_VALUES, load_raw, parse_catalog  # noqa: E402
from ctcap import ENCODINGS, TS_RE, fmt_ts, iter_containers, parse_log  # noqa: E402


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

class Target:
    """One quantity we want to locate, with the value it held at a given time."""

    __slots__ = ("key", "kind", "value", "text")

    def __init__(self, key: str, kind: str, value=None, text: str = ""):
        self.key = key
        self.kind = kind          # "num" or "str"
        self.value = value
        self.text = text


def parse_at(spec: str) -> tuple[str, Optional[float]]:
    """Split 'path@HH:MM:SS' into (path, seconds_since_midnight)."""
    if "@" in spec:
        path, _, at = spec.rpartition("@")
        m = TS_RE.search("[" + at.strip("[]") + "]")
        if not m:
            raise SystemExit("could not parse timestamp in %r (want HH:MM:SS)" % spec)
        h, mi, s, frac = m.groups()
        secs = (int(h) * 3600 + int(mi) * 60 + int(s)
                + (int(frac) / (10 ** len(frac)) if frac else 0))
        return path, secs
    st = os.stat(spec)
    t = dt.datetime.fromtimestamp(st.st_mtime)
    return spec, t.hour * 3600 + t.minute * 60 + t.second


def build_targets(raw: dict, catalog: list[dict], include_unmapped: bool,
                  want_strings: bool) -> list[Target]:
    """Turn one diagnostics payload into the set of values to search for."""
    by_key = {f["key"]: f for f in catalog}
    out: list[Target] = []
    seen: set[tuple[str, str, float]] = set()

    def add_num(key: str, kind: str, v: float):
        sig = (key, kind, round(v, 6))
        if sig not in seen:
            seen.add(sig)
            out.append(Target(key, "num", v, kind))

    for key, v in raw.items():
        if not key.startswith("ct"):
            continue
        spec = by_key.get(key)

        if isinstance(v, str):
            if not want_strings:
                continue
            s = v.strip()
            if s and "�" not in s and len(s) >= 4:
                out.append(Target(key, "str", None, s))
            continue

        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if v in SENTINEL_VALUES:
            continue

        if spec is not None:
            # The cloud's own scale, and the raw wire integer. The bus may use
            # either, or something else entirely - that is what we are testing.
            scale = spec.get("scale", 1.0) or 1.0
            add_num(key, "cloud-scaled", v * scale)
            add_num(key, "cloud-raw", float(v))
        elif include_unmapped:
            add_num(key, "raw", float(v))
            add_num(key, "raw/10", v / 10.0)

    return out


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

Slot = tuple[str, str, int, str]   # stream, container, offset, encoding


_INDEX_CACHE: dict = {}


def _slot_index(frames, use_mdi: bool):
    """Decode every slot in `frames` once, sorted by value.

    Matching used to decode every byte offset under every encoding again for
    each target. That is fine on the synthetic fixture and hopeless on a real
    capture window: hundreds of frames x ~150 offsets x 19 encodings x hundreds
    of targets. Now each export's window is decoded once (identical payloads
    only once), and each target is a binary search over the sorted values.

    main() passes the same `frames` list for every target of an export, so the
    cache keys on that object's identity.
    """
    if _INDEX_CACHE.get("frames") is frames and _INDEX_CACHE.get("mdi") == use_mdi:
        return _INDEX_CACHE["values"], _INDEX_CACHE["slots"], _INDEX_CACHE["blobs"]
    seen = set()
    blobs = []
    pairs = []
    for fr in frames:
        for cname, blob in iter_containers(fr, use_mdi):
            key = (fr.stream, cname, blob)
            if key in seen:
                continue
            seen.add(key)
            blobs.append(key)
            for enc, (width, fn) in ENCODINGS.items():
                for i in range(0, max(0, len(blob) - width + 1)):
                    try:
                        v = fn(blob, i)
                    except (IndexError, TypeError):
                        continue
                    if v is not None:
                        pairs.append((v, (fr.stream, cname, i, enc)))
    pairs.sort(key=lambda p: p[0])
    values = [p[0] for p in pairs]
    slots = [p[1] for p in pairs]
    _INDEX_CACHE.clear()
    _INDEX_CACHE.update(frames=frames, mdi=use_mdi, values=values, slots=slots, blobs=blobs)
    return values, slots, blobs


def slots_matching(frames, target: Target, tol: float, rel: float,
                   use_mdi: bool) -> set[Slot]:
    """Every (stream, container, offset, encoding) holding this target value."""
    import bisect

    values, slots, blobs = _slot_index(frames, use_mdi)
    hits: set[Slot] = set()

    if target.kind == "str":
        needle = target.text.encode("ascii", "ignore")
        if len(needle) < 4:
            return hits
        for stream, cname, blob in blobs:
            idx = blob.find(needle)
            if idx >= 0:
                hits.add((stream, cname, idx, "ascii"))
        return hits

    want = target.value
    eps = max(tol, rel * abs(want))
    lo = bisect.bisect_left(values, want - eps)
    hi = bisect.bisect_right(values, want + eps)
    hits.update(slots[lo:hi])
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(prog="ctmatch")
    ap.add_argument("logfile")
    ap.add_argument("--fields", required=True,
                    help="path to ha-daikinone fields.py")
    ap.add_argument("--export", action="append", required=True, metavar="PATH[@HH:MM:SS]",
                    help="diagnostics JSON and when it was taken; repeatable")
    ap.add_argument("--window", type=float, default=45.0,
                    help="seconds either side of an export's timestamp (default 45)")
    ap.add_argument("--tol", type=float, default=0.15, help="absolute tolerance")
    ap.add_argument("--rel", type=float, default=0.005, help="relative tolerance")
    ap.add_argument("--mdi", action="store_true",
                    help="parse payloads as MDI/DBID datagrams")
    ap.add_argument("--include-unmapped", action="store_true",
                    help="also target ct* keys ha-daikinone does not map")
    ap.add_argument("--no-strings", action="store_true",
                    help="skip ASCII search for model/serial/firmware")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="device name or id, for entry-level diagnostics files")
    ap.add_argument("--max-candidates", type=int, default=6,
                    help="candidates listed per field (default 6)")
    ap.add_argument("--min-score", type=int, default=1,
                    help="only report fields resolved to this many distinct values")
    # Field values and log lines can contain characters the Windows console
    # codepage cannot encode; replace them rather than crash mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = ap.parse_args()

    frames, warnings = parse_log(args.logfile)
    if not frames:
        print("No frames parsed from %s" % args.logfile)
        return 1
    if warnings:
        print("!! %d truncated log line(s); decodes near them may be wrong\n"
              % len(warnings))

    catalog = parse_catalog(args.fields)

    # key -> slot -> set of distinct target values that slot predicted correctly
    confirmed: dict[str, dict[Slot, set]] = defaultdict(lambda: defaultdict(set))
    # key -> number of exports in which the field was live
    live_in: dict[str, int] = defaultdict(int)
    kinds: dict[str, str] = {}

    print("Capture : %s  (%d frames)" % (args.logfile, len(frames)))
    for spec in args.export:
        path, at = parse_at(spec)
        raw = load_raw(path, args.device)
        window = [f for f in frames
                  if f.ts is not None and abs(f.ts - at) <= args.window]
        print("Export  : %-40s @ %s  -> %d frames in +/-%gs"
              % (os.path.basename(path)[:40], fmt_ts(at), len(window), args.window))
        if not window:
            print("          !! no frames in window; check the timestamp")
            continue

        targets = build_targets(raw, catalog, args.include_unmapped,
                                not args.no_strings)
        for key in {t.key for t in targets}:
            live_in[key] += 1
        for t in targets:
            kinds[t.key] = t.kind
            got = slots_matching(window, t, args.tol, args.rel, args.mdi)
            key_slots = confirmed[t.key]
            marker = t.text if t.kind == "str" else t.value
            for slot in got:
                key_slots[slot].add(marker)

    print()

    # A slot only counts if it matched in EVERY export where the field was live.
    resolved, partial, unfound = [], [], []
    for key, slots in confirmed.items():
        n_live = live_in[key]
        ranked = []
        for slot, values in slots.items():
            # distinct values predicted correctly = the real evidence
            ranked.append((len(values), slot, sorted(values, key=str)))
        ranked.sort(key=lambda r: -r[0])
        if not ranked:
            unfound.append(key)
        elif ranked[0][0] >= max(args.min_score, min(n_live, 2)):
            resolved.append((key, n_live, ranked))
        else:
            partial.append((key, n_live, ranked))
    for key in live_in:
        if key not in confirmed:
            unfound.append(key)

    def dump(rows, header):
        print("=" * 78)
        print("%s  (%d)" % (header, len(rows)))
        print("=" * 78)
        for key, n_live, ranked in sorted(rows, key=lambda r: (-r[2][0][0], r[0])):
            uniq = " *** UNIQUE ***" if len(ranked) == 1 else ""
            print("\n%s   (live in %d export%s)%s"
                  % (key, n_live, "" if n_live == 1 else "s", uniq))
            for score, (stream, cname, off, enc), values in ranked[:args.max_candidates]:
                shown = ", ".join(str(v) for v in values[:4])
                print("    %-10s %-10s @%-3d %-12s  %d distinct  [%s]"
                      % (stream, cname, off, enc, score, shown))
            if len(ranked) > args.max_candidates:
                print("    ... %d more candidates" % (len(ranked) - args.max_candidates))
        print()

    if resolved:
        dump(resolved, "RESOLVED - slot tracked multiple distinct values")
    if partial:
        dump(partial, "WEAK - matched, but only ever at one value (add an export "
                      "at a different operating point)")
    if unfound:
        print("=" * 78)
        print("NOT FOUND IN CAPTURE  (%d)" % len(unfound))
        print("=" * 78)
        print("The cloud has these but no capture byte matched. Either the bus "
              "carries them\nin a message type you are not seeing, or in an "
              "encoding not in ENCODINGS.\n")
        for k in sorted(set(unfound)):
            print("  %s" % k)
        print()

    print("Summary: %d resolved, %d weak, %d not found"
          % (len(resolved), len(partial), len(set(unfound))))
    print("\nA RESOLVED row with one candidate is ready to write into "
          "firmware/packages/.\nCorroborate anything else against "
          "docs/decoded-fields.md before trusting it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
