#!/usr/bin/env python3
"""
ctlog - record an esphome-comfortnet log to a capture file.

Deliberately dependency-free and source-agnostic, because ESPHome often runs as
an add-on inside Home Assistant OS where there is no convenient shell to redirect from.
Three ways to use it:

  1. Wrap the esphome CLI (needs `pip install esphome` on this machine):

         python tools/ctlog.py --device daikin-bus-monitor.local \\
             --config firmware/waveshare-esp32-s3-rs485-can.yaml -o captures/run1.log

  2. Pipe anything into it - including text pasted from the ESPHome add-on's
     dashboard log pane:

         type pasted.txt | python tools/ctlog.py -o captures/run1.log

  3. Mark ground truth while a capture runs. Type a note and press Enter and it
     is written into the log as a timestamped comment, so `ctcap find --at`
     has something to anchor to:

         python tools/ctlog.py --device daikin-bus-monitor.local -o run1.log --notes

Every line is echoed to the terminal as well as the file, so you can watch the
bus while recording.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import threading

TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}")

# `esphome logs` needs the device config to find the API encryption key.
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "firmware", "waveshare-esp32-s3-rs485-can.yaml")


def stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def write_line(out, line: str, echo: bool, add_ts: bool) -> None:
    line = line.rstrip("\r\n")
    # ESPHome already prefixes [HH:MM:SS] when the device has a time source.
    # If it does not (no `time:` component, or a pasted excerpt), add host time
    # so ctcap has something to correlate against.
    if add_ts and not TS_RE.match(line):
        line = "[%s] %s" % (stamp(), line)
    out.write(line + "\n")
    out.flush()
    if echo:
        print(line, flush=True)


def note_reader(out, stop: threading.Event) -> None:
    """Read ground-truth notes from stdin and interleave them into the log."""
    try:
        for raw in sys.stdin:
            if stop.is_set():
                return
            note = raw.strip()
            if not note:
                continue
            line = "[%s] ### GROUND TRUTH: %s" % (stamp(), note)
            out.write(line + "\n")
            out.flush()
            print(line, flush=True)
    except (ValueError, OSError):
        return


def run_esphome(args, out) -> int:
    # The esphome.exe shim is often not on PATH (it isn't on the Windows
    # workstation), so fall back to running it as a module under this Python.
    exe = shutil.which("esphome")
    cmd = [exe, "logs"] if exe else [sys.executable, "-m", "esphome", "logs"]
    cmd.append(args.config or DEFAULT_CONFIG)
    if args.device:
        cmd += ["--device", args.device]

    print("$ " + " ".join(cmd), file=sys.stderr)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)

    stop = threading.Event()
    if args.notes:
        print("Type a ground-truth note and press Enter to mark the log. "
              "Ctrl-C to stop.\n", file=sys.stderr)
        threading.Thread(target=note_reader, args=(out, stop), daemon=True).start()

    n = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            write_line(out, line, echo=not args.quiet, add_ts=args.timestamp)
            n += 1
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\nCaptured %d lines to %s" % (n, args.out), file=sys.stderr)
    return 0


def run_stdin(args, out) -> int:
    n = 0
    try:
        for line in sys.stdin:
            write_line(out, line, echo=not args.quiet, add_ts=args.timestamp)
            n += 1
    except KeyboardInterrupt:
        pass
    print("\nCaptured %d lines to %s" % (n, args.out), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ctlog",
        description="Record an esphome-comfortnet log to a capture file.")
    p.add_argument("-o", "--out", required=True, help="capture file to write")
    p.add_argument("--device", help="device host/IP, e.g. daikin-bus-monitor.local")
    p.add_argument("--config", help="ESPHome yaml to pass to `esphome logs`")
    p.add_argument("--notes", action="store_true",
                   help="read ground-truth notes from stdin and interleave them")
    p.add_argument("--append", action="store_true", help="append instead of truncate")
    p.add_argument("--quiet", action="store_true", help="do not echo to terminal")
    p.add_argument("--timestamp", action="store_true", default=True,
                   help="add host [HH:MM:SS] to lines that lack one (default on)")
    p.add_argument("--no-timestamp", dest="timestamp", action="store_false")
    # Field values and log lines can contain characters the Windows console
    # codepage cannot encode; replace them rather than crash mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = p.parse_args(argv)

    if args.notes and not (args.device or args.config):
        p.error("--notes needs a live capture (--device/--config); stdin is "
                "already consumed when piping.")

    mode = "a" if args.append else "w"
    with open(args.out, mode, encoding="utf-8") as out:
        out.write("# ctlog capture started %s\n"
                  % dt.datetime.now().isoformat(timespec="seconds"))
        if args.device or args.config:
            return run_esphome(args, out)
        return run_stdin(args, out)


if __name__ == "__main__":
    sys.exit(main())
