#!/usr/bin/env python3
"""
ctverify - check a decoded CT-485 field map against cloud diagnostics over time.

ctmatch finds candidate byte positions by how many distinct cloud values a
position happened to hold. That can be fooled by coincidence. This tool takes a
proposed map (SLOT_MAP below) and tests each entry as a time series:

  * decode the slot from every matching frame in the capture
  * for each diagnostics snapshot, compare the cloud value against the most
    recent bus value `lag` seconds earlier, for lags 0..--max-lag
  * report the best lag, the share of snapshots that match within tolerance,
    and the mean error at that lag
  * "window": the share of snapshots whose cloud value appears on the bus at
    any point in the --window seconds before the snapshot

The cloud refreshes about every 60-90 s, unevenly, so a real field typically
scores ~95-100% on both. A coincidental position does not.

    python tools/ctverify.py captures/run1.log \\
        --exports 'captures/exports/run1-*.json'

Snapshot times come from ha.py's filenames (<label>-HHMMSS.json).

Stdlib only.
"""

from __future__ import annotations

import argparse
import bisect
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_cloud_fields import DEFAULT_DEVICE, SENTINEL_VALUES, load_raw  # noqa: E402
from ctcap import _ct_temp, _u16le, parse_log, parse_mdi  # noqa: E402


def u8(b: bytes, i: int):
    return b[i] if i < len(b) else None


def u16le(b: bytes, i: int):
    return _u16le(b, i)


def ct_temp(b: bytes, i: int):
    v = _u16le(b, i)
    return None if v is None else _ct_temp(v)


DECODERS = {"u8": u8, "u16le": u16le, "ct_temp": ct_temp}


# The field map verified on the tested system (DH7VSA2410A heat pump,
# DFVE36CP1300 air handler) from a cooling run with no zone controller in the
# circuit. Other models may place fields differently: edit the entries to
# test your own candidates. docs/decoded-fields.md explains each one.
#
#   key         ha-daikinone / raw cloud key
#   cloud       scale applied to the cloud's raw integer
#   stream      <source node type>/<message type>
#   container   "L<len>" = raw payload of that exact length (the heat pump's
#               0xA0 response has 35 and 60 byte layouts); "dbid:NN" = that MDI
#               datagram
#   offset      byte offset within the container
#   dec         u8 | u16le | ct_temp (ClimateTalk valid/sign/10.4 word, degF)
#   bus         scale applied to the decoded bus value
#   unit        "F" uses a 0.15 degF tolerance; everything else must match exactly
#   status      verified | candidate (single run) | note
SLOT_MAP = [
    # --- heat pump, Get Mfr Generic Data response, 60-byte layout ----------
    dict(key="ctOutdoorPower", cloud=10, stream="05/0xA0", container="L60", offset=8, dec="u8", bus=10, unit="W", status="verified"),
    dict(key="ctIndoorPower", cloud=0.1, stream="05/0xA0", container="L60", offset=10, dec="u16le", bus=0.1, unit="W", status="verified"),
    dict(key="ctCurrentCompressorRPS", cloud=1, stream="05/0xA0", container="L60", offset=14, dec="u8", bus=1, unit="rps", status="verified"),
    dict(key="ctCompressorCurrent", cloud=0.1, stream="05/0xA0", container="L60", offset=15, dec="u8", bus=0.1, unit="A", status="verified"),
    dict(key="ctInverterCurrent", cloud=0.1, stream="05/0xA0", container="L60", offset=18, dec="u8", bus=0.1, unit="A", status="verified"),
    dict(key="ctTargetCompressorspeed", cloud=1, stream="05/0xA0", container="L60", offset=20, dec="u8", bus=1, unit="rps", status="verified"),
    dict(key="ctTargetODFanRPM", cloud=10, stream="05/0xA0", container="L60", offset=22, dec="u8", bus=10, unit="rpm",
         status="verified; target: drops to 0 first at shutdown while byte 27 spins down"),
    dict(key="ctOutdoorAirTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=25, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorFanRPM", cloud=1, stream="05/0xA0", container="L60", offset=27, dec="u8", bus=10, unit="rpm",
         status="verified; actual: still 480 rpm after byte 22 reads 0, reaches 0 a minute later"),
    dict(key="ctOutdoorDischargeTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=28, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorCoilTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=30, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorDefrostSensorTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=32, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorLiquidTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=34, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorSuctionPressure", cloud=1, stream="05/0xA0", container="L60", offset=36, dec="u8", bus=1, unit="psi", status="verified"),
    dict(key="ctOutdoorSuctionTemperature", cloud=0.1, stream="05/0xA0", container="L60", offset=38, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctOutdoorEEVOpening", cloud=1, stream="05/0xA0", container="L60", offset=23, dec="u8", bus=0.5, unit="%",
         status="candidate: only unassigned byte that is 0 at idle and 136 (68%) running; two values seen"),
    dict(key="ctOutdoorCompressorRunTime", cloud=1, stream="05/0xA0", container="L60", offset=40, dec="u16le", bus=1, unit="h",
         status="candidate (value never changed during the run)"),
    dict(key="ctOutdoorRequestedIndoorAirflow", cloud=1, stream="05/0xA0", container="L60", offset=43, dec="u16le", bus=1, unit="cfm", status="verified"),

    # --- heat pump, Get Mfr Generic Data response, 35-byte layout ----------
    dict(key="ctOutdoorOperationMode", cloud=1, stream="05/0xA0", container="L35", offset=3, dec="u8", bus=1, unit="", status="verified"),
    dict(key="ctOutdoorDischargeTemperature", cloud=0.1, stream="05/0xA0", container="L35", offset=26, dec="ct_temp", bus=1, unit="F",
         status="note: weak second copy; use the L60 @28 one"),

    # --- air handler ------------------------------------------------------
    dict(key="ctAHEEVOpenRate", cloud=1, stream="03/0xA0", container="L9", offset=5, dec="u8", bus=1, unit="%",
         status="verified raw byte; half-percent (200 = fully open when heating), cloud value unscaled too"),
    dict(key="ctAHCurrentIndoorAirflow", cloud=1, stream="03/0x82", container="dbid:00", offset=12, dec="u16le", bus=1, unit="cfm", status="verified"),
    dict(key="ctAHSuperHeatValue", cloud=0.1, stream="03/0x82", container="dbid:01", offset=9, dec="ct_temp", bus=1, unit="F",
         status="verified by physics: cloud value = AH suction temp - R-32 saturation at ctAHPressureSensor; "
                "all 36 snapshots within the capture window, fixed-lag fit weak (~36%)"),
    dict(key="ctAHSubCoolValue", cloud=0.1, stream="03/0x82", container="dbid:01", offset=39, dec="ct_temp", bus=1, unit="F",
         status="verified by physics: cloud value = saturation - AH liquid temp; fixed-lag fit weak (~42%)"),
    dict(key="ctAHLiquidTemperature", cloud=0.1, stream="03/0x87", container="dbid:02", offset=0, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctAHSuctionTemperature", cloud=0.1, stream="03/0x87", container="dbid:02", offset=2, dec="ct_temp", bus=1, unit="F", status="verified"),
    dict(key="ctAHPressureSensor", cloud=1, stream="03/0x87", container="dbid:02", offset=12, dec="u8", bus=1, unit="psi",
         status="verified: indoor refrigerant pressure, psig (tracks outdoor suction pressure); word has bit 15 valid"),
    dict(key="ctAHFanMotorRPM", cloud=1, stream="03/0xA0", container="L9", offset=3, dec="u16le", bus=1, unit="rpm", status="verified"),
    dict(key="ctAHFanCurrentDemandStatus", cloud=1, stream="03/0x82", container="dbid:00", offset=4, dec="u8", bus=1, unit="",
         status="verified (half-percent); identical to @15 in every sample"),
    dict(key="ctAHFanRequestedDemand", cloud=1, stream="03/0x82", container="dbid:00", offset=15, dec="u8", bus=1, unit="",
         status="verified (half-percent); identical to @4 in every sample"),

    # --- heat pump, more 60-byte fields ------------------------------------
    dict(key="ctOutdoorFrequencyInPercent", cloud=1, stream="05/0xA0", container="L60", offset=3, dec="u8", bus=1, unit="",
         status="verified (half-percent)"),
    dict(key="ctInverterFinTemp", cloud=10, stream="05/0xA0", container="L60", offset=19, dec="u8", bus=1, unit="",
         status="verified raw byte; encoding inferred as degC + 40 (settled idle tracks outdoor air; plain degC or degF "
                "are physically implausible; Daikin's fin overheat trip is 95 C)"),
]

SNAP_TIME_RE = re.compile(r"-(\d{2})(\d{2})(\d{2})\.json$")


def bus_series(frames, entry) -> list[tuple[float, float]]:
    dec = DECODERS[entry["dec"]]
    container = entry["container"]
    out = []
    for fr in frames:
        if fr.stream != entry["stream"] or fr.ts is None:
            continue
        if container.startswith("L"):
            if len(fr.payload) != int(container[1:]):
                continue
            blob = fr.payload
        else:
            tag = int(container.split(":")[1], 16)
            datagrams, trailing = parse_mdi(fr.payload)
            if trailing:
                continue
            blob = next((d.data for d in datagrams if d.tag == tag), None)
            if blob is None:
                continue
        v = dec(blob, entry["offset"])
        if v is not None:
            out.append((fr.ts, v * entry["bus"]))
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(prog="ctverify")
    ap.add_argument("logfile")
    ap.add_argument("--exports", required=True,
                    help="glob of diagnostics snapshots named <label>-HHMMSS.json")
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--max-lag", type=int, default=180)
    ap.add_argument("--window", type=int, default=150)
    ap.add_argument("--only", help="regex over field keys")
    args = ap.parse_args()

    frames, _ = parse_log(args.logfile)
    snaps = []
    for path in sorted(glob.glob(args.exports)):
        m = SNAP_TIME_RE.search(path)
        if not m:
            continue
        h, mi, s = (int(x) for x in m.groups())
        snaps.append((h * 3600 + mi * 60 + s, load_raw(path, args.device)))
    if not snaps:
        print("no snapshots matched %r" % args.exports)
        return 1
    print("capture: %s (%d frames)   snapshots: %d" % (args.logfile, len(frames), len(snaps)))

    rx = re.compile(args.only) if args.only else None
    print("\n%-34s %-22s %4s %5s %5s %6s %8s %7s  %s"
          % ("field", "slot", "n", "dist", "lag", "match", "meanErr", "window", "status"))
    for e in SLOT_MAP:
        if rx and not rx.search(e["key"]):
            continue
        slot = "%s %s @%d" % (e["stream"], e["container"], e["offset"])
        ser = bus_series(frames, e)
        truth = [(ts, raw[e["key"]] * e["cloud"]) for ts, raw in snaps
                 if isinstance(raw.get(e["key"]), int) and not isinstance(raw.get(e["key"]), bool)
                 and raw[e["key"]] not in SENTINEL_VALUES]
        if not ser or not truth:
            print("%-34s %-22s  %s" % (e["key"], slot,
                                       "no bus frames" if not ser else "no live cloud values"))
            continue
        times = [t for t, _ in ser]
        tol = 0.15 if e["unit"] == "F" else 1e-6

        best = None
        for lag in range(0, args.max_lag + 1, 5):
            errs = []
            for ts, want in truth:
                i = bisect.bisect_right(times, ts - lag) - 1
                if i >= 0:
                    errs.append(abs(ser[i][1] - want))
            if errs:
                mean = sum(errs) / len(errs)
                hit = sum(1 for x in errs if x <= tol) / len(errs)
                if best is None or (hit, -mean) > (best[1], -best[2]):
                    best = (lag, hit, mean, len(errs))

        in_window = 0
        for ts, want in truth:
            lo = bisect.bisect_left(times, ts - args.window)
            hi = bisect.bisect_right(times, ts)
            if any(abs(ser[j][1] - want) <= tol for j in range(lo, hi)):
                in_window += 1

        lag, hit, mean, n = best
        print("%-34s %-22s %4d %5d %5d %5.0f%% %8.2f %6.0f%%  %s" % (
            e["key"], slot, n, len({round(w, 3) for _, w in truth}), lag, hit * 100,
            mean, 100.0 * in_window / len(truth), e["status"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
