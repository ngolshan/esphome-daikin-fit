#!/usr/bin/env python3
"""
ha - talk to Home Assistant's REST API for Daikin capture and decoding work.

Reads a long-lived access token from a file (never from the command line, so it
does not land in shell history or process listings) and never prints it.

    # token: created in HA under Profile -> Security -> Long-lived access tokens,
    # saved to %USERPROFILE%\\.ha_token (override with HA_TOKEN_FILE)
    # URL: --url, or the HA_URL environment variable

    python tools/ha.py ping
    python tools/ha.py climate                          # list thermostats
    python tools/ha.py set climate.living_room --mode cool --temp 68
    python tools/ha.py entries --domain daikinone         # find the entry id
    python tools/ha.py diag --entry <entry_id> --label A1
    python tools/ha.py poll-diag --entry <entry_id> --every 60 --count 30
    python tools/ha.py entities --pattern 'coil|compressor|eev'
    python tools/ha.py history --start 14:05 --end 14:45 --pattern '...' -o hist.csv

Use `entries` to find the config entry id. The ids embedded in a diagnostics
download's filename are NOT usable here: for daikinone the first is the entry
title, not its id, and device-level diagnostics 404 via the API. Entry-level
diagnostics (no --device) return the same data.raw payload.

Every diagnostics snapshot is written under captures/exports/ with its fetch
time in the filename, and the matching ctmatch argument is printed, e.g.
    --export captures/exports/A1-141522.json@14:15:22

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT_DIR = os.path.join(REPO, "captures", "exports")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def load_token() -> str:
    path = os.environ.get("HA_TOKEN_FILE") or os.path.join(os.path.expanduser("~"), ".ha_token")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            tok = fh.read().strip()
    except FileNotFoundError:
        sys.exit("No token file at %s. Create a long-lived access token in HA and "
                 "save it there (or set HA_TOKEN_FILE)." % path)
    if not tok:
        sys.exit("Token file %s is empty." % path)
    # Validate locally so a bad file is never sent. Report only safe metadata
    # (length, presence of control characters) - never the content itself.
    if any(ord(c) < 32 or ord(c) == 127 for c in tok):
        sys.exit("Token file %s does not hold a Home Assistant token: it is %d "
                 "character(s) including control characters. The paste most likely "
                 "did not take (Ctrl+V can type a literal control character at a "
                 "hidden prompt). Re-save the file and try again." % (path, len(tok)))
    # HA long-lived access tokens are JWTs: three dot-separated base64url parts.
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", tok):
        sys.exit("Token file %s does not look like a Home Assistant long-lived "
                 "access token (%d characters; expected three dot-separated "
                 "base64url segments). Re-save the file and try again."
                 % (path, len(tok)))
    return tok


class HA:
    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _redact(self, text: str) -> str:
        """Strip the token from text HA sends back.

        HA's 400 responses can echo the offending request header verbatim, so
        any error body must be scrubbed before it is printed. Redact before
        truncating, so a token cut at the boundary cannot leak partially.
        """
        text = text.replace(self.token, "<token>")
        return re.sub(r"(?i)(bearer\s+)[^\s'\"]+", r"\1<redacted>", text)

    def _req(self, method: str, path: str, body=None, raw: bool = False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        # Token goes in the header only - never in the URL.
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("HA rejected the token (401). Was it revoked or mistyped?")
            detail = self._redact(e.read().decode("utf-8", "replace"))[:300]
            sys.exit("HA returned HTTP %d for %s %s: %s" % (e.code, method, path, detail))
        except urllib.error.URLError as e:
            sys.exit("Could not reach %s: %s" % (self.base, e.reason))
        return payload if raw else (json.loads(payload) if payload else None)

    def get(self, path: str, raw: bool = False):
        return self._req("GET", path, raw=raw)

    def post(self, path: str, body: dict):
        return self._req("POST", path, body)


def local_hms(t: dt.datetime) -> str:
    return t.astimezone().strftime("%H:%M:%S")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ping(ha: HA, args) -> None:
    msg = ha.get("/api/")
    cfg = ha.get("/api/config")
    print("%s  (Home Assistant %s, time zone %s)"
          % (msg.get("message"), cfg.get("version"), cfg.get("time_zone")))


def cmd_entries(ha: HA, args) -> None:
    for e in ha.get("/api/config/config_entries/entry"):
        if args.domain and e.get("domain") != args.domain:
            continue
        print("%-28s %-22s %-10s %s"
              % (e["entry_id"], e.get("domain"), e.get("state"), e.get("title")))


def cmd_climate(ha: HA, args) -> None:
    for st in ha.get("/api/states"):
        if not st["entity_id"].startswith("climate."):
            continue
        a = st.get("attributes", {})
        print("%-40s state=%-8s current=%s target=%s low=%s high=%s  [%s]"
              % (st["entity_id"], st["state"], a.get("current_temperature"),
                 a.get("temperature"), a.get("target_temp_low"),
                 a.get("target_temp_high"), a.get("friendly_name")))
        if args.verbose:
            print("    " + json.dumps(a, default=str))


def wait_for_mode(ha: HA, entity: str, mode: str, timeout: float = 90.0) -> None:
    """Block until the entity reports `mode`.

    daikinone applies a mode change through Daikin's cloud, and decides whether a
    single set_temperature value is a heat or a cool set point from its cached
    mode. Sent too early, the call fails inside the integration with "Invalid
    thermostat mode and set temperature combination" (HTTP 500). Seen on
    2026-09-12; the cached mode caught up within about a minute.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ha.get("/api/states/" + entity)["state"] == mode:
            return
        time.sleep(3)
    sys.exit("%s did not report mode %r within %ds; not sending the set point"
             % (entity, mode, timeout))


def cmd_set(ha: HA, args) -> None:
    before = ha.get("/api/states/" + args.entity)
    a = before.get("attributes", {})
    print("before: state=%s target=%s low=%s high=%s"
          % (before["state"], a.get("temperature"), a.get("target_temp_low"),
             a.get("target_temp_high")))
    if args.mode:
        ha.post("/api/services/climate/set_hvac_mode",
                {"entity_id": args.entity, "hvac_mode": args.mode})
        if args.temp is not None or args.low is not None or args.high is not None:
            print("waiting for %s to report mode %r..." % (args.entity, args.mode))
            wait_for_mode(ha, args.entity, args.mode)
    if args.temp is not None:
        ha.post("/api/services/climate/set_temperature",
                {"entity_id": args.entity, "temperature": args.temp})
    if args.low is not None or args.high is not None:
        body = {"entity_id": args.entity}
        if args.low is not None:
            body["target_temp_low"] = args.low
        if args.high is not None:
            body["target_temp_high"] = args.high
        ha.post("/api/services/climate/set_temperature", body)
    time.sleep(2)
    after = ha.get("/api/states/" + args.entity)
    a = after.get("attributes", {})
    print("after:  state=%s target=%s low=%s high=%s"
          % (after["state"], a.get("temperature"), a.get("target_temp_low"),
             a.get("target_temp_high")))


def fetch_diag(ha: HA, entry: str, device: str, label: str) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    if device:
        url = "/api/diagnostics/config_entry/%s/device/%s" % (
            urllib.parse.quote(entry), urllib.parse.quote(device))
    else:
        url = "/api/diagnostics/config_entry/%s" % urllib.parse.quote(entry)
    blob = ha.get(url, raw=True)
    now = dt.datetime.now()
    name = "%s-%s.json" % (label, now.strftime("%H%M%S"))
    path = os.path.join(EXPORT_DIR, name)
    with open(path, "wb") as fh:
        fh.write(blob)
    try:
        raw = json.loads(blob).get("data", {}).get("raw", {})
        n = len(raw)
    except ValueError:
        n = -1
    rel = os.path.relpath(path, REPO).replace("\\", "/")
    print("%s  %d bytes, %d raw keys   --export %s@%s"
          % (now.strftime("%H:%M:%S"), len(blob), n, rel, now.strftime("%H:%M:%S")))
    return path


def cmd_diag(ha: HA, args) -> None:
    fetch_diag(ha, args.entry, args.device, args.label)


def cmd_poll_diag(ha: HA, args) -> None:
    i = 0
    try:
        while args.count == 0 or i < args.count:
            fetch_diag(ha, args.entry, args.device, args.label)
            i += 1
            if args.count and i >= args.count:
                break
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass
    print("took %d snapshot(s)" % i)


def matching_entities(ha: HA, pattern: str) -> list[dict]:
    rx = re.compile(pattern, re.I)
    out = []
    for st in ha.get("/api/states"):
        name = st.get("attributes", {}).get("friendly_name", "")
        if rx.search(st["entity_id"]) or rx.search(name):
            out.append(st)
    return out


def cmd_entities(ha: HA, args) -> None:
    for st in sorted(matching_entities(ha, args.pattern), key=lambda s: s["entity_id"]):
        a = st.get("attributes", {})
        print("%-60s %12s %-6s  %s"
              % (st["entity_id"], st["state"], a.get("unit_of_measurement", ""),
                 a.get("friendly_name", "")))


def parse_clock(hhmm: str) -> dt.datetime:
    parts = [int(x) for x in hhmm.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return dt.datetime.now().astimezone().replace(
        hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)


def cmd_history(ha: HA, args) -> None:
    ents = [s["entity_id"] for s in matching_entities(ha, args.pattern)]
    if not ents:
        sys.exit("No entities match %r." % args.pattern)
    start, end = parse_clock(args.start), parse_clock(args.end)
    q = urllib.parse.urlencode({
        "end_time": end.isoformat(),
        "filter_entity_id": ",".join(ents),
        "minimal_response": "",
        "no_attributes": "",
    })
    series = ha.get("/api/history/period/%s?%s"
                    % (urllib.parse.quote(start.isoformat()), q))
    rows = []
    for track in series or []:
        if not track:
            continue
        eid = track[0].get("entity_id")
        for pt in track:
            eid = pt.get("entity_id", eid)
            ts = dt.datetime.fromisoformat(pt["last_changed"].replace("Z", "+00:00"))
            rows.append((ts, eid, pt.get("state")))
    rows.sort()
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(["local_time", "entity_id", "state"])
        for ts, eid, state in rows:
            w.writerow([local_hms(ts), eid, state])
    finally:
        if args.out:
            out.close()
    print("%d state changes across %d entities" % (len(rows), len(ents)), file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ha")
    ap.add_argument("--url", default=os.environ.get("HA_URL"),
                    help="Home Assistant base URL (or set HA_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping").set_defaults(func=cmd_ping)

    sp = sub.add_parser("entries", help="list config entries (to find an entry id)")
    sp.add_argument("--domain")
    sp.set_defaults(func=cmd_entries)

    sp = sub.add_parser("climate")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_climate)

    sp = sub.add_parser("set")
    sp.add_argument("entity")
    sp.add_argument("--mode", help="off, cool, heat, heat_cool, ...")
    sp.add_argument("--temp", type=float)
    sp.add_argument("--low", type=float)
    sp.add_argument("--high", type=float)
    sp.set_defaults(func=cmd_set)

    for name, fn in (("diag", cmd_diag), ("poll-diag", cmd_poll_diag)):
        sp = sub.add_parser(name)
        sp.add_argument("--entry", required=True)
        sp.add_argument("--device", help="device id; omit for entry-level diagnostics")
        sp.add_argument("--label", default="snap")
        if name == "poll-diag":
            sp.add_argument("--every", type=float, default=60.0)
            sp.add_argument("--count", type=int, default=0, help="0 = until Ctrl-C")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("entities")
    sp.add_argument("--pattern", required=True)
    sp.set_defaults(func=cmd_entities)

    sp = sub.add_parser("history")
    sp.add_argument("--start", required=True, help="HH:MM[:SS] today, local time")
    sp.add_argument("--end", required=True)
    sp.add_argument("--pattern", required=True)
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_history)

    # Entity names and units contain characters the Windows console codepage
    # cannot encode (e.g. the micro sign); never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = ap.parse_args(argv)
    if not args.url:
        ap.error("give --url or set HA_URL")
    args.func(HA(args.url, load_token()), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
