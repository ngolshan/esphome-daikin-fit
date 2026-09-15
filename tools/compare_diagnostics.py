#!/usr/bin/env python3
"""
Compare several Home Assistant `daikinone` diagnostics exports side by side.

Two questions this answers:

  * does a field that reads as a sentinel at idle become live under load?
  * do two thermostats on the same system see the same equipment?

Both matter for deciding how much of the bus decoding needs manual ground
truth. Usage:

    python tools/compare_diagnostics.py a.json b.json c.json --labels idle load up
    python tools/compare_diagnostics.py *.json --filter compressor --changed-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_cloud_fields import DEFAULT_DEVICE, load_raw  # noqa: E402

SENTINELS = {255, 65535, 32767, 4294967295}


def render(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, int) and v in SENTINELS:
        return "SENT"
    if isinstance(v, str):
        s = v.strip()
        return repr(s) if s and "�" not in s else "BLANK"
    if isinstance(v, float):
        return "%g" % v
    return str(v)


def is_dead(v) -> bool:
    if v is None:
        return True
    if isinstance(v, int) and v in SENTINELS:
        return True
    if isinstance(v, str) and (not v.strip() or "�" in v.strip()):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--labels", nargs="*", help="short column labels, in file order")
    ap.add_argument("--filter", default=r"^ct|outdoor|compressor|coil|eev|inverter"
                                        r"|discharge|liquid|suction|defrost|refrig|odu",
                    help="regex over key names (default: equipment fields)")
    ap.add_argument("--changed-only", action="store_true",
                    help="only keys whose rendered value differs across files")
    ap.add_argument("--revived-only", action="store_true",
                    help="only keys dead in the FIRST file but alive in a later one")
    ap.add_argument("--all-keys", action="store_true", help="ignore --filter")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="device name or id, for entry-level diagnostics files")
    # Field values and log lines can contain characters the Windows console
    # codepage cannot encode; replace them rather than crash mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = ap.parse_args()

    raws = [load_raw(f, args.device) for f in args.files]
    labels = args.labels or [os.path.basename(f)[:18] for f in args.files]
    if len(labels) != len(raws):
        ap.error("got %d labels for %d files" % (len(labels), len(raws)))

    pat = None if args.all_keys else re.compile(args.filter, re.I)
    keys = sorted({k for r in raws for k in r})
    if pat:
        keys = [k for k in keys if pat.search(k)]

    rows = []
    for k in keys:
        vals = [r.get(k) for r in raws]
        rendered = [render(v) for v in vals]
        if args.changed_only and len(set(rendered)) == 1:
            continue
        if args.revived_only and not (is_dead(vals[0])
                                      and any(not is_dead(v) for v in vals[1:])):
            continue
        rows.append((k, rendered))

    w = max([len(k) for k, _ in rows] + [3])
    colw = max([max(len(x) for x in r) for _, r in rows] + [len(l) for l in labels]) + 2
    print("%-*s %s" % (w, "key", "".join("%-*s" % (colw, l) for l in labels)))
    print("-" * (w + colw * len(labels)))
    for k, rendered in rows:
        print("%-*s %s" % (w, k, "".join("%-*s" % (colw, x) for x in rendered)))
    print("\n%d rows" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
