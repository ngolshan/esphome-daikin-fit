#!/usr/bin/env python3
"""
Evaluate a Home Assistant `daikinone` diagnostics JSON against ha-daikinone's
field catalog, to see which ct* telemetry the cloud still reports.

This decides how much manual ground-truth work the bus decoding needs: every
field that is live here is a field we can correlate automatically instead of
transcribing off the thermostat screen.

    python tools/check_cloud_fields.py <diagnostics.json> --fields <path to fields.py>

Sentinels and scales are parsed straight out of ha-daikinone's fields.py so
this stays honest if upstream changes them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

SENTINELS = {"U8": 255, "U16": 65535, "I16": 32767, "U32": 4294967295}

# The same thing as a value set. Keep these distinct: `x in SENTINELS` tests
# names, not values, and confusing the two silently treats every sentinel as a
# real reading.
SENTINEL_VALUES = frozenset(SENTINELS.values())

# Default device: in entry-level diagnostics, pick the device reporting the most
# live outdoor unit (ctOutdoor*) fields. Behind a zone controller only one
# thermostat carries equipment data. Pass --device to choose by name or id.
DEFAULT_DEVICE = None


def load_raw(path: str, device: str | None = DEFAULT_DEVICE) -> dict:
    """Return the flat field dict from a daikinone diagnostics file.

    Two shapes exist:
      * device-page download from the HA UI: data.raw is the flat dict
      * entry-level diagnostics from the API (tools/ha.py): data.raw is a list
        with one element per Daikin device, {id, name, model, ..., data};
        pick the device by id, or by name case-insensitively
    """
    doc = json.load(open(path, encoding="utf-8"))
    raw = doc.get("data", {}).get("raw")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        if device is None:
            def live_outdoor(dev) -> int:
                data = dev.get("data") if isinstance(dev, dict) else None
                if not isinstance(data, dict):
                    return -1
                return sum(1 for k, v in data.items() if k.startswith("ctOutdoor")
                           and isinstance(v, (int, float)) and v not in SENTINEL_VALUES)
            best = max(raw, key=live_outdoor, default=None)
            if best is None or live_outdoor(best) < 0:
                raise SystemExit("%s: no device data in entry-level diagnostics" % path)
            return best["data"]
        names = []
        for dev in raw:
            if not isinstance(dev, dict):
                continue
            name = str(dev.get("name", ""))
            names.append(name)
            matched = device == dev.get("id") or device.lower() == name.lower()
            if matched and isinstance(dev.get("data"), dict):
                return dev["data"]
        raise SystemExit("%s: no device %r in entry-level diagnostics (devices: %s)"
                         % (path, device, ", ".join(names)))
    raise SystemExit("%s: no data.raw in this diagnostics file" % path)

# e.g.  F_OD_COIL_TEMP = TempField("ctOutdoorCoilTemperature")
#       F_OD_FAN_TARGET_RPM = IntField("ctTargetODFanRPM", Sentinel.U8, 10)
#       F_OD_INVERTER_FIN_TEMP = TempField("ctInverterFinTemp", Sentinel.U8, scale=1, unit="C")
DECL_RE = re.compile(
    r"^(?P<const>F_[A-Z0-9_]+)\s*=\s*(?P<cls>\w+Field)\((?P<args>.*?)\)\s*$",
    re.MULTILINE,
)


def parse_catalog(path: str) -> list[dict]:
    src = open(path, encoding="utf-8").read()
    out = []
    for m in DECL_RE.finditer(src):
        args = m.group("args")
        key_m = re.search(r'"([^"]+)"', args)
        if not key_m:
            continue
        cls = m.group("cls")
        sent_m = re.search(r"Sentinel\.(\w+)", args)
        scale_m = re.search(r"scale\s*=\s*([\d.]+)", args)
        if not scale_m:
            # positional scale: third arg after key and sentinel
            parts = [p.strip() for p in args.split(",")]
            if len(parts) >= 3 and re.fullmatch(r"[\d.]+", parts[2]):
                scale_m = re.match(r"([\d.]+)", parts[2])
        unit_m = re.search(r'unit\s*=\s*"(\w+)"', args)

        if cls == "TempField":
            sentinel = SENTINELS[sent_m.group(1)] if sent_m else SENTINELS["I16"]
            scale = float(scale_m.group(1)) if scale_m else 0.1
            unit = unit_m.group(1) if unit_m else "F"
        elif cls == "RuntimeField":
            sentinel = SENTINELS[sent_m.group(1)] if sent_m else SENTINELS["U32"]
            scale, unit = 1.0, "h"
        elif cls in ("IntField", "FloatField"):
            sentinel = SENTINELS[sent_m.group(1)] if sent_m else None
            scale = float(scale_m.group(1)) if scale_m else 1.0
            unit = ""
        else:  # StringField, PercentField, CelsiusField, UnitTypeField
            sentinel, scale, unit = None, 1.0, ""

        out.append({"const": m.group("const"), "cls": cls, "key": key_m.group(1),
                    "sentinel": sentinel, "scale": scale, "unit": unit})
    return out


def classify(field: dict, raw) -> tuple[str, str]:
    """Return (status, rendered_value)."""
    if raw is None:
        return "MISSING", "-"
    cls = field["cls"]

    if cls == "StringField":
        s = str(raw).strip()
        if not s or "�" in s:
            return "UNAVAIL", repr(str(raw))
        return "LIVE", s

    if cls == "UnitTypeField":
        if isinstance(raw, int) and raw != SENTINELS["U8"]:
            return "LIVE", "present (0x%02X)" % raw
        return "UNAVAIL", str(raw)

    if cls == "PercentField":
        if isinstance(raw, int) and 0 <= raw <= 100:
            return "LIVE", "%d %%" % raw
        return "UNAVAIL", str(raw)

    if cls == "CelsiusField":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return "UNAVAIL", str(raw)
        return ("LIVE", "%.1f C" % v) if -60 <= v <= 80 else ("UNAVAIL", str(raw))

    if not isinstance(raw, int):
        return "UNAVAIL", str(raw)
    if field["sentinel"] is not None and raw == field["sentinel"]:
        return "SENTINEL", "%d (unavailable)" % raw

    val = raw * field["scale"]
    unit = field["unit"]
    if cls == "RuntimeField":
        return "LIVE", "%d h" % raw
    if unit in ("F", "C"):
        return "LIVE", "%.1f %s  (raw %d)" % (val, unit, raw)
    if field["scale"] != 1.0:
        return "LIVE", "%g  (raw %d, x%g)" % (val, raw, field["scale"])
    return "LIVE", "%d" % raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("diagnostics")
    ap.add_argument("--fields", required=True, help="path to ha-daikinone fields.py")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="device name or id, for entry-level diagnostics files "
                         "(default: the one with the most live outdoor unit fields)")
    ap.add_argument("--only-ct", action="store_true",
                    help="restrict to ct* equipment fields")
    # Field values and log lines can contain characters the Windows console
    # codepage cannot encode; replace them rather than crash mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = ap.parse_args()

    raw = load_raw(args.diagnostics, args.device)

    catalog = parse_catalog(args.fields)
    if args.only_ct:
        catalog = [f for f in catalog if f["key"].startswith("ct")]

    buckets: dict[str, list] = {"LIVE": [], "SENTINEL": [], "UNAVAIL": [], "MISSING": []}
    for f in catalog:
        status, rendered = classify(f, raw.get(f["key"]))
        buckets[status].append((f, rendered))

    print("Diagnostics : %s" % args.diagnostics)
    print("raw keys    : %d" % len(raw))
    print("catalog     : %d fields\n" % len(catalog))

    for status, header in (
        ("LIVE", "LIVE - cloud is still reporting these"),
        ("SENTINEL", "SENTINEL - key present but equipment is not reporting a value"),
        ("UNAVAIL", "UNAVAILABLE - present but fails the plausibility check"),
        ("MISSING", "MISSING - key absent from the payload entirely"),
    ):
        rows = buckets[status]
        print("=" * 78)
        print("%s  (%d)" % (header, len(rows)))
        print("=" * 78)
        for f, rendered in sorted(rows, key=lambda r: r[0]["key"]):
            print("  %-45s %-12s %s" % (f["key"], f["cls"].replace("Field", ""), rendered))
        print()

    n_live = len(buckets["LIVE"])
    print("Summary: %d live / %d total (%.0f%%)"
          % (n_live, len(catalog), 100.0 * n_live / max(1, len(catalog))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
