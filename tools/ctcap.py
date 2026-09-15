#!/usr/bin/env python3
"""
ctcap - analyse ClimateTalk / CT-485 frame captures from esphome-comfortnet logs.

The ComfortNet ESPHome component prints every validated frame at DEBUG level as
a pipe-delimited row (comfortnet.cpp:136-141). This tool parses those rows back
into structured frames and helps you work out which payload bytes carry which
physical quantity, by correlating against ground truth read off the thermostat's
installer/diagnostics screens.

Typical workflow:

    # 1. record a capture while stepping the system through known states
    python tools/ctlog.py daikin-bus-monitor.local -o captures/2026-09-08.log

    # 2. see what is on the bus
    python tools/ctcap.py summary captures/2026-09-08.log

    # 3. you saw "Outdoor Coil Temp 38.4" on the thermostat at 18:22:31.
    #    find every byte offset / encoding that held that value right then:
    python tools/ctcap.py find captures/2026-09-08.log --value 38.4 --at 18:22:31 --mdi

    # 4. confirm a candidate is stable and tracks reality over the whole capture
    python tools/ctcap.py bytes captures/2026-09-08.log --stream 05/0x87 --mdi --words

    # 5. export for plotting against Home Assistant history
    python tools/ctcap.py csv captures/2026-09-08.log --stream 05/0x82 --mdi -o coil.csv

Stdlib only. No install required.
"""

from __future__ import annotations

import argparse
import csv as csvmod
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# [18:22:31] or [18:22:31.123]
TS_RE = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?\]")

# The frame row emitted by comfortnet.cpp. Written permissively: field widths
# and padding have shifted across upstream revisions, so match on structure
# (the pipe-separated 0x fields) rather than on exact spacing.
ROW_RE = re.compile(
    r"\b(?P<dir>TX|RX)\b\s*\|"
    r"\s*0x(?P<dest>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<src>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<subnet>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<method>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<params>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<src_node>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<msg_type>[0-9A-Fa-f]+)\s*\|"
    r"\s*0x(?P<pkt_num>[0-9A-Fa-f]+)\s*\|"
    r"\s*(?P<length>\d+)\s*\|"
    r"\s*0x(?P<crc>[0-9A-Fa-f]+)\s*\|"
    r"\s*(?P<payload>.*?)\s*$"
)

# ESPHome's format_hex_pretty() joins bytes with '.' and appends " (N)".
# Other code paths use spaces or colons, so accept any of them.
HEX_TRAILER_RE = re.compile(r"\s*\(\d+\)\s*$")


def parse_hex_blob(text: str) -> bytes:
    """Parse '01.A2.FF (3)' / '01 A2 FF' / '01:A2:FF' into bytes."""
    text = HEX_TRAILER_RE.sub("", text.strip())
    if not text:
        return b""
    out = bytearray()
    for tok in re.split(r"[.: \t]+", text):
        if not tok:
            continue
        if len(tok) % 2 or not re.fullmatch(r"[0-9A-Fa-f]+", tok):
            # Not a hex blob after all (truncated or wrapped log line).
            return bytes(out)
        for i in range(0, len(tok), 2):
            out.append(int(tok[i:i + 2], 16))
    return bytes(out)


@dataclass
class Frame:
    lineno: int
    ts: Optional[float]          # seconds since midnight
    ts_raw: str
    direction: str
    dest: int
    src: int
    subnet: int
    method: int
    params: int
    src_node: int
    msg_type: int
    pkt_num: int
    length: int
    crc: int
    payload: bytes

    @property
    def stream(self) -> str:
        """Identity of the logical data stream this frame belongs to."""
        return "%02X/0x%02X" % (self.src_node, self.msg_type)

    @property
    def truncated(self) -> bool:
        return len(self.payload) != self.length


def parse_ts(line: str) -> tuple[Optional[float], str]:
    m = TS_RE.search(line)
    if not m:
        return None, ""
    h, mi, s, frac = m.groups()
    val = int(h) * 3600 + int(mi) * 60 + int(s)
    if frac:
        val += int(frac) / (10 ** len(frac))
    return val, m.group(0).strip("[]")


def parse_log(path: str) -> tuple[list[Frame], list[str]]:
    frames: list[Frame] = []
    warnings: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = ANSI_RE.sub("", raw.rstrip("\n"))
            if "|" not in line:
                continue
            m = ROW_RE.search(line)
            if not m:
                continue
            g = m.groupdict()
            ts, ts_raw = parse_ts(line)
            fr = Frame(
                lineno=lineno,
                ts=ts,
                ts_raw=ts_raw,
                direction=g["dir"],
                dest=int(g["dest"], 16),
                src=int(g["src"], 16),
                subnet=int(g["subnet"], 16),
                method=int(g["method"], 16),
                params=int(g["params"], 16),
                src_node=int(g["src_node"], 16),
                msg_type=int(g["msg_type"], 16),
                pkt_num=int(g["pkt_num"], 16),
                length=int(g["length"]),
                crc=int(g["crc"], 16),
                payload=parse_hex_blob(g["payload"]),
            )
            if fr.truncated:
                warnings.append(
                    "line %d: declared len %d but parsed %d bytes - log line "
                    "likely truncated (raise logger tx_buffer_size)"
                    % (lineno, fr.length, len(fr.payload))
                )
            frames.append(fr)
    return frames, warnings


# --------------------------------------------------------------------------
# MDI (DBID datagram) decoding
# --------------------------------------------------------------------------

@dataclass
class Datagram:
    tag: int
    length: int
    data: bytes
    offset: int  # byte offset of `data` within the parent payload


def parse_mdi(payload: bytes) -> tuple[list[Datagram], bytes]:
    """Split an MDI payload into DBID datagrams.

    Mirrors Comfortnet::read_mdi (comfortnet.h:200-214): a flat sequence of
    (tag, len, data[len]) records. Returns (datagrams, trailing_bytes).
    A non-empty trailing slice means the payload did not decode cleanly and
    probably is not MDI-structured.
    """
    out: list[Datagram] = []
    i = 0
    n = len(payload)
    while i + 2 <= n:
        tag = payload[i]
        ln = payload[i + 1]
        i += 2
        if i + ln > n:
            return out, payload[i - 2:]
        out.append(Datagram(tag=tag, length=ln, data=payload[i:i + ln], offset=i))
        i += ln
    return out, payload[i:]


# --------------------------------------------------------------------------
# Value encodings
# --------------------------------------------------------------------------

def _ct_temp(raw: int) -> Optional[float]:
    """ClimateTalk 'valid/sign/10.4' sensor word.

    bit15 = valid, bit14 = negative, bits13..4 = whole, bits3..0 = 1/16ths.
    Matches the decoder in comfortnet_heat_pump.yaml.
    """
    if not raw & (1 << 15):
        return None
    whole = (raw >> 4) & 0x3FF
    frac = (raw & 0x0F) / 16.0
    val = whole + frac
    return -val if raw & (1 << 14) else val


def _u16le(b: bytes, i: int) -> Optional[int]:
    return b[i] | (b[i + 1] << 8) if i + 1 < len(b) else None


def _u16be(b: bytes, i: int) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if i + 1 < len(b) else None


def _s8(v: int) -> int:
    return v - 256 if v > 127 else v


def _s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def _wrap16(fn: Callable[[int], Optional[float]], be: bool = False):
    def inner(b: bytes, i: int) -> Optional[float]:
        v = _u16be(b, i) if be else _u16le(b, i)
        return None if v is None else fn(v)
    return inner


# name -> (width_in_bytes, fn(payload, offset) -> Optional[float])
#
# The scales here are chosen to cover both encodings we expect to meet:
#
#   * the CT-485 wire encodings the ComfortNet component already decodes
#     (u8/2 for demand percentages, ct_temp_* for sensor words), and
#   * the encodings the Daikin cloud API uses for the same quantities, taken
#     from ha-daikinone's field catalog (deci-degF int16 temperatures,
#     deca-watt power, deci-amp currents, x10 fan RPM). See
#     docs/decoded-fields.md - the bus may or may not agree with the cloud,
#     which is exactly what the capture work is for.
ENCODINGS: dict[str, tuple[int, Callable[[bytes, int], Optional[float]]]] = {
    "u8":         (1, lambda b, i: float(b[i])),
    "u8/2":       (1, lambda b, i: b[i] / 2.0),          # CT demand/actual %
    "u8/10":      (1, lambda b, i: b[i] / 10.0),         # deci-amp currents
    "u8*10":      (1, lambda b, i: b[i] * 10.0),         # ctTargetODFanRPM
    "s8":         (1, lambda b, i: float(_s8(b[i]))),
    "u16le":      (2, _wrap16(lambda v: float(v))),
    "u16be":      (2, _wrap16(lambda v: float(v), be=True)),
    "u16le/10":   (2, _wrap16(lambda v: v / 10.0)),      # deci-watt, deci-amp
    "u16be/10":   (2, _wrap16(lambda v: v / 10.0, be=True)),
    "u16le/16":   (2, _wrap16(lambda v: v / 16.0)),
    "u16le/100":  (2, _wrap16(lambda v: v / 100.0)),
    "u16le*10":   (2, _wrap16(lambda v: v * 10.0)),      # ctOutdoorPower (deca-watt)
    "u16be*10":   (2, _wrap16(lambda v: v * 10.0, be=True)),
    "s16le":      (2, _wrap16(lambda v: float(_s16(v)))),
    "s16be":      (2, _wrap16(lambda v: float(_s16(v)), be=True)),
    "s16le/10":   (2, _wrap16(lambda v: _s16(v) / 10.0)),   # deci-degF temps
    "s16be/10":   (2, _wrap16(lambda v: _s16(v) / 10.0, be=True)),
    "ct_temp_le": (2, _wrap16(_ct_temp)),
    "ct_temp_be": (2, _wrap16(_ct_temp, be=True)),
}


def iter_containers(fr: Frame, use_mdi: bool) -> Iterator[tuple[str, bytes]]:
    """Yield (container_name, bytes) for a frame."""
    if not use_mdi:
        yield "raw/L%d" % len(fr.payload), fr.payload
        return
    datagrams, trailing = parse_mdi(fr.payload)
    if not datagrams or trailing:
        yield "raw/L%d" % len(fr.payload), fr.payload
        return
    for dg in datagrams:
        yield "dbid:%02X" % dg.tag, dg.data


# --------------------------------------------------------------------------
# Node / message naming (from components/comfortnet/types.h)
# --------------------------------------------------------------------------

NODE_TYPES = {
    0x00: "Any", 0x01: "Thermostat", 0x02: "Gas Furnace", 0x03: "Air Handler",
    0x04: "Air Conditioner", 0x05: "Heat Pump", 0x06: "Electric Furnace",
    0x07: "Package Gas", 0x08: "Package Electric", 0x09: "Crossover (OBBI)",
    0x0A: "Secondary Compressor", 0x0B: "Air Exchanger", 0x0C: "Unitary Control",
    0x0D: "Dehumidifier", 0x0E: "Electronic Air Cleaner", 0x0F: "ERV",
    0x10: "Humidifier (Evap)", 0x11: "Humidifier (Steam)", 0x12: "HRV",
    0x13: "IAQ Analyzer", 0x14: "Media Air Cleaner", 0x15: "Zone Control",
    0x16: "Zone User Interface", 0x17: "Boiler", 0x18: "Water Heater Gas",
    0x19: "Water Heater Electric", 0x1A: "Water Heater Commercial",
    0x1B: "Pool Heater", 0x1C: "Ceiling Fan", 0x1D: "Gateway",
    0x1E: "Diagnostic Device", 0x1F: "Lighting Control", 0x20: "Security System",
    0x21: "UV Light", 0x22: "Weather Data Device", 0x23: "Whole House Fan",
    0x24: "Solar Inverter", 0x25: "Zone Damper", 0x26: "Zone Temperature Control",
    0x27: "Temperature Sensor", 0x28: "Occupancy Sensor",
    0xA5: "Network Coordinator",
}

MSG_TYPES = {
    0x01: "Get Configuration", 0x81: "Get Configuration Resp",
    0x02: "Get Status", 0x82: "Get Status Resp",
    0x03: "Set Control Command", 0x83: "Set Control Command Resp",
    0x05: "Set Diagnostics", 0x85: "Set Diagnostics Resp",
    0x06: "Get Diagnostics", 0x86: "Get Diagnostics Resp",
    0x07: "Get Sensor Data", 0x87: "Get Sensor Data Resp",
    0x0D: "Set Identification", 0x8D: "Set Identification Resp",
    0x0E: "Get Identification", 0x8E: "Get Identification Resp",
    0x10: "Set App Shared Data", 0x90: "Set App Shared Data Resp",
    0x11: "Get App Shared Data", 0x91: "Get App Shared Data Resp",
    0x12: "Set Mfr Device Data", 0x92: "Set Mfr Device Data Resp",
    0x13: "Get Mfr Device Data", 0x93: "Get Mfr Device Data Resp",
    0x14: "Set Network Node List", 0x94: "Set Network Node List Resp",
    0x1D: "DMA Read", 0x9D: "DMA Read Resp",
    0x1F: "Set Mfr Generic Data", 0x9F: "Set Mfr Generic Data Resp",
    0x20: "Get Mfr Generic Data", 0xA0: "Get Mfr Generic Data Resp",
    0x00: "R2R / Response", 0x75: "Network State Req", 0xF5: "Network State Resp",
    0x76: "Address Confirmation", 0xF6: "Address Confirmation Resp",
    0x77: "Token Offer", 0xF7: "Token Offer Resp",
    0x78: "Version Announcement",
    0x79: "Node Discovery", 0xF9: "Node Discovery Resp",
    0x7A: "Set Address", 0xFA: "Set Address Resp",
    0x7B: "Get Node ID", 0xFB: "Get Node ID Resp",
    0x41: "Get User Menu", 0xC1: "Get User Menu Resp",
    0x44: "Get Shared Data From App", 0xC4: "Get Shared Data From App Resp",
}


def node_name(t: int) -> str:
    return NODE_TYPES.get(t, "Unknown 0x%02X" % t)


def msg_name(t: int) -> str:
    return MSG_TYPES.get(t, "Unknown 0x%02X" % t)


def fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "--:--:--"
    h = int(ts // 3600)
    m = int((ts % 3600) // 60)
    return "%02d:%02d:%06.3f" % (h, m, ts % 60)


def select_frames(frames: list[Frame], stream: Optional[str]) -> list[Frame]:
    """Filter by a 'NN/0xMM' stream id (case and 0x insensitive)."""
    if not stream:
        return frames
    want = stream.strip().lower().replace("0x", "")
    return [f for f in frames
            if ("%02x/%02x" % (f.src_node, f.msg_type)) == want]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_summary(frames: list[Frame], args) -> None:
    if not frames:
        print("No frames parsed. Is this an esphome-comfortnet DEBUG log?")
        return

    span = [f.ts for f in frames if f.ts is not None]
    print("Frames parsed : %d" % len(frames))
    if span:
        print("Capture window: %s .. %s  (%.0fs)"
              % (fmt_ts(min(span)), fmt_ts(max(span)), max(span) - min(span)))
    print()

    print("Nodes seen (by source address):")
    addr_types: dict[int, Counter] = defaultdict(Counter)
    for fr in frames:
        addr_types[fr.src][fr.src_node] += 1
    for addr in sorted(addr_types):
        types = ", ".join("%s (0x%02X) x%d" % (node_name(t), t, c)
                          for t, c in addr_types[addr].most_common())
        print("  addr 0x%02X: %s" % (addr, types))
    print()

    streams: dict[tuple[int, int], list[Frame]] = defaultdict(list)
    for fr in frames:
        streams[(fr.src_node, fr.msg_type)].append(fr)

    print("Streams (source node type / message type):")
    print("  %-11s %-24s %-26s %6s %-14s %s"
          % ("stream", "node", "message", "count", "lens", "window"))
    for (node, msg), grp in sorted(streams.items(), key=lambda kv: -len(kv[1])):
        lens = sorted({len(f.payload) for f in grp})
        lens_s = ",".join(str(x) for x in lens[:4]) + ("..." if len(lens) > 4 else "")
        tsv = [f.ts for f in grp if f.ts is not None]
        window = "%s..%s" % (fmt_ts(min(tsv)), fmt_ts(max(tsv))) if tsv else ""
        print("  %-11s %-24s %-26s %6d %-14s %s"
              % ("%02X/0x%02X" % (node, msg), node_name(node), msg_name(msg),
                 len(grp), lens_s, window))

    print()
    print("MDI datagrams by stream (payloads that decode cleanly as DBID records):")
    any_mdi = False
    for (node, msg), grp in sorted(streams.items()):
        tags: dict[int, Counter] = defaultdict(Counter)
        ok = 0
        for fr in grp:
            dgs, trailing = parse_mdi(fr.payload)
            if not dgs or trailing:
                continue
            ok += 1
            for dg in dgs:
                tags[dg.tag][dg.length] += 1
        if ok and tags:
            any_mdi = True
            desc = "  ".join(
                "dbid 0x%02X len=%s" % (t, "/".join(str(x) for x in sorted(c)))
                for t, c in sorted(tags.items()))
            print("  %-11s (%d/%d clean): %s"
                  % ("%02X/0x%02X" % (node, msg), ok, len(grp), desc))
    if not any_mdi:
        print("  (none - no payloads parsed cleanly as MDI)")


def cmd_bytes(frames: list[Frame], args) -> None:
    sel = select_frames(frames, args.stream)
    if not sel:
        print("No frames for stream %r. Run `summary` to list streams." % args.stream)
        return

    # container -> offset -> list of (ts, byte)
    series: dict[str, dict[int, list[tuple[Optional[float], int]]]] = defaultdict(
        lambda: defaultdict(list))
    # container -> list of (ts, blob), for word decoding
    blobs: dict[str, list[tuple[Optional[float], bytes]]] = defaultdict(list)

    for fr in sel:
        for cname, blob in iter_containers(fr, args.mdi):
            blobs[cname].append((fr.ts, blob))
            for i, b in enumerate(blob):
                series[cname][i].append((fr.ts, b))

    for cname in sorted(series):
        offsets = series[cname]
        print("\n=== %s  %s  (%d frames) ===" % (args.stream, cname, len(sel)))
        print("%4s %5s %5s %4s %4s  %s"
              % ("off", "const", "uniq", "min", "max", "values / decode hint"))
        for i in sorted(offsets):
            vals = [v for _, v in offsets[i]]
            uniq = sorted(set(vals))
            const = len(uniq) == 1
            if const and not args.all:
                continue
            shown = ",".join("%02X" % v for v in uniq[:8])
            if len(uniq) > 8:
                shown += ",... (%d distinct)" % len(uniq)
            extra = ""
            if not const:
                extra = "   [u8/2: %.1f..%.1f%%]" % (min(vals) / 2, max(vals) / 2)
            print("%4d %5s %5d %4d %4d  %s%s"
                  % (i, "yes" if const else "no", len(uniq),
                     min(vals), max(vals), shown, extra))

        if args.words:
            print("\n  16-bit candidates in %s:" % cname)
            pairs = blobs[cname]
            maxlen = max((len(b) for _, b in pairs), default=0)
            found = False
            for i in range(0, max(0, maxlen - 1)):
                for enc in ("u16le", "u16be", "ct_temp_le", "ct_temp_be"):
                    _, fn = ENCODINGS[enc]
                    vals = [x for _, b in pairs
                            if (x := fn(b, i)) is not None]
                    if not vals or len(set(vals)) < 2:
                        continue
                    lo, hi = min(vals), max(vals)
                    if enc.startswith("ct_temp") and not (-60 <= lo and hi <= 250):
                        continue  # implausible as a temperature
                    found = True
                    print("    @%-3d %-11s %10.3f .. %-10.3f (%d distinct)"
                          % (i, enc, lo, hi, len(set(vals))))
            if not found:
                print("    (none varied)")


def cmd_find(frames: list[Frame], args) -> None:
    """Find every (stream, container, offset, encoding) holding a target value."""
    target = args.value
    tol = args.tol

    at = None
    if args.at:
        m = TS_RE.search("[" + args.at.strip("[]") + "]")
        if not m:
            print("Could not parse --at %r; expected HH:MM:SS" % args.at)
            return
        h, mi, s, frac = m.groups()
        at = (int(h) * 3600 + int(mi) * 60 + int(s)
              + (int(frac) / (10 ** len(frac)) if frac else 0))

    if at is not None:
        sel = [f for f in frames
               if f.ts is not None and abs(f.ts - at) <= args.window]
        if not sel:
            print("No frames within +/-%gs of %s." % (args.window, args.at))
            return
        print("Searching %d frames within +/-%gs of %s for %g (tol %g).\n"
              % (len(sel), args.window, args.at, target, tol))
    else:
        sel = frames
        print("Searching all %d frames for %g (tol %g).\n" % (len(sel), target, tol))

    hits: dict[tuple[str, str, int, str], list[float]] = defaultdict(list)
    for fr in sel:
        for cname, blob in iter_containers(fr, args.mdi):
            for enc, (width, fn) in ENCODINGS.items():
                for i in range(0, max(0, len(blob) - width + 1)):
                    try:
                        v = fn(blob, i)
                    except (IndexError, TypeError):
                        continue
                    if v is not None and abs(v - target) <= tol:
                        hits[(fr.stream, cname, i, enc)].append(v)

    if not hits:
        print("No match. Things to try:")
        print("  * widen --tol (thermostat displays are usually rounded)")
        print("  * widen --window, or drop --at to search the whole capture")
        print("  * toggle --mdi (status & sensor payloads are MDI-wrapped,")
        print("    identification and mfr-data payloads usually are not)")
        print("  * the value may live in a stream you are not polling yet")
        return

    print("%-10s %-10s %4s %-12s %5s  %s"
          % ("stream", "container", "off", "encoding", "hits", "observed"))
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0][3]))
    for (stream, cname, off, enc), vals in ranked[:args.limit]:
        rng = ("%.3f" % vals[0] if min(vals) == max(vals)
               else "%.3f..%.3f" % (min(vals), max(vals)))
        print("%-10s %-10s %4d %-12s %5d  %s"
              % (stream, cname, off, enc, len(vals), rng))

    if len(ranked) > args.limit:
        print("\n(%d more; raise --limit)" % (len(ranked) - args.limit))
    print("\nNarrow these down by capturing a second ground-truth point at a "
          "different value\nand intersecting the candidate lists.")


def cmd_csv(frames: list[Frame], args) -> None:
    sel = select_frames(frames, args.stream)
    if not sel:
        print("No frames for stream %r." % args.stream)
        return

    widths: dict[str, int] = {}
    for fr in sel:
        for cname, blob in iter_containers(fr, args.mdi):
            widths[cname] = max(widths.get(cname, 0), len(blob))

    cols = ["ts", "lineno", "src_addr", "stream"]
    for cname in sorted(widths):
        for i in range(widths[cname]):
            cols.append("%s[%d]" % (cname, i))
            if args.decode:
                cols.append("%s[%d]:u8/2" % (cname, i))
                if i + 1 < widths[cname]:
                    cols.append("%s[%d]:u16le" % (cname, i))
                    cols.append("%s[%d]:ct_temp_le" % (cname, i))

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csvmod.writer(out)
        w.writerow(cols)
        for fr in sel:
            row = dict.fromkeys(cols, "")
            row["ts"] = fmt_ts(fr.ts)
            row["lineno"] = fr.lineno
            row["src_addr"] = "0x%02X" % fr.src
            row["stream"] = fr.stream
            for cname, blob in iter_containers(fr, args.mdi):
                for i, b in enumerate(blob):
                    row["%s[%d]" % (cname, i)] = b
                    if not args.decode:
                        continue
                    row["%s[%d]:u8/2" % (cname, i)] = b / 2.0
                    k = "%s[%d]:u16le" % (cname, i)
                    if k in row:
                        v = _u16le(blob, i)
                        row[k] = "" if v is None else v
                        ct = _ct_temp(v) if v is not None else None
                        row["%s[%d]:ct_temp_le" % (cname, i)] = (
                            "" if ct is None else round(ct, 4))
            w.writerow([row[c] for c in cols])
    finally:
        if args.out:
            out.close()
            print("Wrote %d rows to %s" % (len(sel), args.out))


def cmd_mdi(frames: list[Frame], args) -> None:
    sel = select_frames(frames, args.stream)
    if not sel:
        print("No frames for stream %r." % args.stream)
        return
    for fr in sel[:args.limit]:
        dgs, trailing = parse_mdi(fr.payload)
        print("\n[%s] line %d  %s  %s / %s  len=%d"
              % (fmt_ts(fr.ts), fr.lineno, fr.stream,
                 node_name(fr.src_node), msg_name(fr.msg_type), fr.length))
        if not dgs:
            print("  (no MDI structure) %s" % fr.payload.hex("."))
            continue
        for dg in dgs:
            print("  dbid 0x%02X len=%-3d @%-3d %s"
                  % (dg.tag, dg.length, dg.offset, dg.data.hex(".")))
        if trailing:
            print("  trailing %dB: %s  <- does not decode as MDI"
                  % (len(trailing), trailing.hex(".")))


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ctcap",
        description="Analyse ClimateTalk/CT-485 captures from "
                    "esphome-comfortnet DEBUG logs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("logfile")
        sp.add_argument("--mdi", action="store_true",
                        help="parse payloads as MDI/DBID datagrams "
                             "(use for 0x82 status and 0x87 sensor data)")
        return sp

    sp = common(sub.add_parser("summary",
                               help="inventory of nodes, streams and MDI layout"))
    sp.set_defaults(func=cmd_summary)

    sp = common(sub.add_parser("bytes",
                               help="per-offset variance analysis for one stream"))
    sp.add_argument("--stream", required=True, help="e.g. 05/0x87")
    sp.add_argument("--all", action="store_true", help="include constant bytes")
    sp.add_argument("--words", action="store_true", help="also try 16-bit decodes")
    sp.set_defaults(func=cmd_bytes)

    sp = common(sub.add_parser("find",
                               help="locate a known value from the thermostat display"))
    sp.add_argument("--value", type=float, required=True)
    sp.add_argument("--tol", type=float, default=0.05,
                    help="match tolerance (default 0.05; raise for rounded displays)")
    sp.add_argument("--at", help="ground-truth timestamp HH:MM:SS")
    sp.add_argument("--window", type=float, default=30.0,
                    help="seconds either side of --at (default 30)")
    sp.add_argument("--limit", type=int, default=40)
    sp.set_defaults(func=cmd_find)

    sp = common(sub.add_parser("csv", help="export a stream as a time series"))
    sp.add_argument("--stream", required=True)
    sp.add_argument("--decode", action="store_true",
                    help="add u8/2, u16le and ct_temp columns")
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_csv)

    sp = common(sub.add_parser("mdi", help="dump MDI datagram structure frame by frame"))
    sp.add_argument("--stream")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_mdi)

    args = p.parse_args(argv)
    frames, warnings = parse_log(args.logfile)
    if warnings and args.cmd == "summary":
        print("!! %d truncated line(s) detected; first few:" % len(warnings))
        for w in warnings[:5]:
            print("   " + w)
        print()
    args.func(frames, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
