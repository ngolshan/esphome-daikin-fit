#!/usr/bin/env python3
"""
Generate a synthetic esphome-comfortnet DEBUG log for exercising ctcap.py.

This is a test fixture, not real bus data. It fabricates frames in exactly the
format comfortnet.cpp:136-141 emits, with known values planted at known offsets
so the analyzer's output can be checked against ground truth.

    python tests/make_test_capture.py -o tests/example-synthetic.log

Planted ground truth (see PLANTED below):
    heat pump 0x87 sensor data, dbid 0x01, offset 0, ct_temp_le
        -> outdoor coil temp, ramps 38.4375 .. 46.6875 F
    heat pump 0x82 status,      dbid 0x00, offset 2, u8/2
        -> heat demand, ramps 0 .. 100 %
"""

from __future__ import annotations

import argparse
import json
import os
import random

PLANTED = """
Planted ground truth in this fixture:
  05/0x87  dbid:01 @0  ct_temp_le   outdoor coil temp   38.4375 .. 46.6875 F
  05/0x82  dbid:00 @2  u8/2         heat demand         0 .. 100 %
  05/0x87  dbid:00 @0  ct_temp_le   outdoor air temp    31.0000 .. 33.5000 F
  03/0x82  dbid:00 @13 u16le        airflow             350 .. 1150 CFM
"""


def ct_temp_bytes(value: float) -> bytes:
    """Encode a float into the ClimateTalk valid/sign/10.4 word, little endian."""
    neg = value < 0
    v = abs(value)
    whole = int(v)
    frac = int(round((v - whole) * 16)) & 0x0F
    raw = 0x8000 | (whole & 0x3FF) << 4 | frac
    if neg:
        raw |= 0x4000
    return bytes([raw & 0xFF, (raw >> 8) & 0xFF])


def mdi(records: list[tuple[int, bytes]]) -> bytes:
    """Pack (dbid_tag, data) records into an MDI payload."""
    out = bytearray()
    for tag, data in records:
        out.append(tag)
        out.append(len(data))
        out += data
    return bytes(out)


def hex_pretty(data: bytes) -> str:
    """Reproduce ESPHome's format_hex_pretty()."""
    return ".".join("%02X" % b for b in data) + " (%d)" % len(data)


def frame(ts: float, line_src: int, dest: int, src_addr: int, src_node: int,
          msg_type: int, payload: bytes, direction: str = "RX") -> str:
    h = int(ts // 3600)
    m = int((ts % 3600) // 60)
    s = int(ts % 60)
    crc = random.randint(0, 0xFFFF)
    return ("[%02d:%02d:%02d][D][comfortnet:%d]: "
            "%s  | 0x%02X | 0x%02X | 0x03   | 0x00 | 0x0000 | 0x%02X    | "
            "0x%02X    | 0x80   | %-3d | 0x%04X   | %s"
            % (h, m, s, line_src, direction, dest, src_addr, src_node,
               msg_type, len(payload), crc, hex_pretty(payload)))


HEADER = ("[%s][D][comfortnet:136]: Dir | Dest | Src  | Subnet | Meth | Params "
          "| SrcNode | MsgType | PktNum | Len | Checksum | Payload HEX")


def write_export(path: str, coil: float, oat: float, heat_demand: int,
                 airflow: int) -> None:
    """Emit a minimal daikinone diagnostics payload consistent with the capture.

    Values are encoded the way the *cloud* encodes them (deci-degF for
    temperatures, raw uint8 for x0.5 demands), which is deliberately not how the
    synthetic bus frames encode them. That is the whole point: ctmatch has to
    bridge the two.
    """
    raw = {
        # sentinels, so the "not found" path gets exercised too
        "ctCurrentCompressorRPS": 65535,
        "ctOutdoorEEVOpening": 255,
        # planted, recoverable
        "ctOutdoorCoilTemperature": int(round(coil * 10)),
        "ctOutdoorAirTemperature": int(round(oat * 10)),
        "ctOutdoorHeatRequestedDemand": heat_demand,
        "ctAHCurrentIndoorAirflow": airflow,
        "ctOutdoorModelNoCharacter1_15": "DH7VSA2410     ",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"data": {"raw": raw}}, fh, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="tests/example-synthetic.log")
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--start", default="18:20:00")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--exports", metavar="DIR",
                    help="also write synthetic diagnostics exports here, for "
                         "exercising ctmatch.py")
    ap.add_argument("--export-steps", default="0,20,39",
                    help="capture steps to snapshot as exports")
    args = ap.parse_args()

    random.seed(args.seed)
    hh, mm, ss = (int(x) for x in args.start.split(":"))
    t0 = hh * 3600 + mm * 60 + ss

    lines: list[str] = [
        "[%s][I][app:029]: Running through tests..." % args.start,
        "[%s][I][comfortnet:411]: Joined network as address: 0x04" % args.start,
    ]

    for step in range(args.steps):
        ts = t0 + step * 10
        tstr = "%02d:%02d:%02d" % (int(ts // 3600), int(ts % 3600 // 60), int(ts % 60))

        # Ramp the planted quantities so byte-variance analysis has something
        # to chew on, with a little noise on unrelated bytes.
        frac = step / max(1, args.steps - 1)
        coil = 38.4375 + frac * 8.25          # outdoor coil temp
        oat = 31.0 + frac * 2.5               # outdoor air temp
        heat_demand = int(round(frac * 100 * 2))   # u8/2 encoding -> 0..100 %
        airflow = int(350 + frac * 800)            # CFM, u16le

        lines.append(HEADER % tstr)

        # --- heat pump (node 0x05, addr 0x03) status ------------------------
        hp_status = mdi([(0x00, bytes([
            0x00,                 # 0  critical fault
            0x00,                 # 1  minor fault
            heat_demand,          # 2  heat demand      <- PLANTED
            0x00,                 # 3  cool demand
            0x00,                 # 4  dehum demand
            heat_demand,          # 5  heat actual
            0x00,                 # 6  cool actual
            0x00,                 # 7  defrost demand
            min(200, heat_demand + 20),  # 8 fan demand
            0x00, 0x00,           # 9,10 reserved
            0x00,                 # 11 dehum actual
        ]))])
        lines.append(frame(ts, 138, 0xFF, 0x03, 0x05, 0x82, hp_status))

        # --- heat pump sensor data ------------------------------------------
        hp_sensor = mdi([
            (0x00, ct_temp_bytes(oat)),    # outdoor air temp
            (0x01, ct_temp_bytes(coil)),   # outdoor coil temp  <- PLANTED
        ])
        lines.append(frame(ts + 1, 138, 0xFF, 0x03, 0x05, 0x87, hp_sensor))

        # --- air handler (node 0x03, addr 0x02) status ----------------------
        ah_status = mdi([(0x00, bytes([
            0x00, 0x00,
            heat_demand, 0x00, 0x00,
            min(200, heat_demand + 20),
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            airflow & 0xFF, (airflow >> 8) & 0xFF,   # 13,14 airflow <- PLANTED
            heat_demand, 0x00,
            min(200, heat_demand + 20),
            0x00, 0x00, 0x00, 0x00,
        ]))])
        lines.append(frame(ts + 2, 138, 0xFF, 0x02, 0x03, 0x82, ah_status))

        # --- some non-MDI traffic, so summary has to discriminate -----------
        if step % 7 == 0:
            lines.append(frame(ts + 3, 138, 0xFF, 0x03, 0x05, 0x8E,
                               bytes([0x02, 0x00, 0x11, 0x22])))
        if step % 3 == 0:
            lines.append(frame(ts + 4, 138, 0x00, 0xFF, 0xA5, 0x77, b"", "RX"))

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("Wrote %d lines to %s" % (len(lines), args.out))

    if args.exports:
        os.makedirs(args.exports, exist_ok=True)
        print()
        for step in (int(x) for x in args.export_steps.split(",")):
            if not 0 <= step < args.steps:
                continue
            ts = t0 + step * 10
            frac = step / max(1, args.steps - 1)
            path = os.path.join(args.exports, "synthetic-step%02d.json" % step)
            write_export(path,
                         coil=38.4375 + frac * 8.25,
                         oat=31.0 + frac * 2.5,
                         heat_demand=int(round(frac * 100 * 2)),
                         airflow=int(350 + frac * 800))
            # the 0x87 frame for this step sits one second after the 0x82
            print("  --export %s@%02d:%02d:%02d"
                  % (path, int(ts // 3600), int(ts % 3600 // 60), int(ts % 60) + 1))

    print(PLANTED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
