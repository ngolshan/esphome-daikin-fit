# Decoding other models

The decoded fields were verified on one heat pump and one air handler. This is
how to check them on different Daikin Fit equipment, or find fields that moved,
using the same method.

The idea: a payload byte becomes a field only when you can tie it to a number
you independently know. Daikin's cloud supplies those numbers, through the
[ha-daikinone](https://github.com/zlangbert/ha-daikinone) Home Assistant
integration, so a bus capture taken alongside cloud snapshots can be matched
field by field.

## What you need

- The monitor wired and running ([`wiring.md`](wiring.md)).
- Home Assistant with ha-daikinone set up for your thermostat, and a
  long-lived access token saved to `%USERPROFILE%\.ha_token` (Windows) or
  `~/.ha_token`, or wherever `HA_TOKEN_FILE` points. Set `HA_URL` to your
  Home Assistant address.
- A local copy of ha-daikinone's field catalog,
  `custom_components/daikinone/client/fields.py`.
- Python 3.10 or later, and the ESPHome CLI for recording (`pip install
  esphome`). The tools use only the standard library.

## Best conditions

- **No zone controller in the circuit, if you can manage it.** With a Daikin
  thermostat connected directly to the equipment, the cloud reports nearly
  every field, and the thermostat requests the vendor data blocks itself. A
  zone controller hides most compressor and inverter fields from the cloud, so
  there is nothing to match them against. See [`how-it-works.md`](how-it-works.md).
- **Make the values move.** Bytes that never change cannot be identified. Walk
  the system through states and hold each one for a few minutes:
  1. system off, settled (baseline)
  2. a call from cold, while the compressor ramps up (the most valuable part)
  3. steady running for 10 minutes or more
  4. fan only
  5. a defrost, if you can catch one
  6. the other mode, if the season allows
- **Several snapshots at different operating points.** Three values that
  differ beat thirty that are the same.

## Record

Run these in two terminals over the same period.

The bus capture, with `--notes` so you can type what the thermostat shows and
have it timestamped in the log:

```bash
python tools/ctlog.py --device daikin-bus-monitor.local -o captures/run1.log --notes
```

Cloud diagnostics every 60 s (use `entries` first to find the entry id):

```bash
python tools/ha.py entries --domain daikinone
python tools/ha.py poll-diag --entry <entry_id> --label run1 --every 60 --count 30
```

Snapshots are saved under `captures/exports/` with their fetch time in the
filename. Captures and exports can contain equipment serial numbers; scrub
them before sharing.

## Find candidates

What is on the bus, and which streams carry DBID blocks:

```bash
python tools/ctcap.py summary captures/run1.log
```

Which bytes move in a stream, with 16-bit and temperature-word readings:

```bash
python tools/ctcap.py bytes captures/run1.log --stream 05/0xA0 --words
```

Match every live cloud field against the capture at once:

```bash
python tools/ctmatch.py captures/run1.log \
    --fields <ha-daikinone>/custom_components/daikinone/client/fields.py \
    --export captures/exports/run1-141522.json@14:15:22 \
    --export captures/exports/run1-142530.json@14:25:30 \
    --export captures/exports/run1-143541.json@14:35:41 \
    --mdi --include-unmapped
```

`ha.py` prints the matching `--export` argument for each snapshot. A field
with a single candidate is nearly settled; several candidates need another
snapshot at a different value.

For a value read off the thermostat instead of the cloud, anchor it to the
note's time, and repeat at a second, different value:

```bash
python tools/ctcap.py find captures/run1.log --value 38.4 --tol 0.1 --at 14:22:31 --mdi
```

Thermostat displays round, so match the tolerance to the display: a screen
reading of `38` needs `--tol 0.5`.

## Verify

`tools/ctverify.py` tests a proposed map as a time series against every
snapshot. Its `SLOT_MAP` holds the map from the tested system; edit the
entries to your candidates, then run:

```bash
python tools/ctverify.py captures/run1.log --exports 'captures/exports/run1-*.json'
```

A real field matches about 95–100 % of snapshots at its best lag, because the
cloud refreshes unevenly every 60–90 s. A coincidence does not.

Before trusting a decode, check that it:

- matches known values at several different operating points
- tracks across a range, not just one value
- stays within a physically plausible range over a whole capture
- holds across a reboot of the monitor and of the equipment
- does not move when an unrelated quantity moves

## Turn it into a sensor

Add the field to `firmware/daikin-fit.yaml` as an `on_packet` or
`on_control_command` lambda, following the existing ones, and name it with the
convention in [`decoded-fields.md`](decoded-fields.md).

## Share it

Open an issue with your outdoor unit and indoor unit model numbers, what
matched and what did not, and scrubbed captures if you can.

## Checking the tools

`tests/make_test_capture.py` generates a synthetic log with values planted at
known offsets. Use it after changing `ctcap.py`:

```bash
python tests/make_test_capture.py -o tests/example-synthetic.log
python tools/ctcap.py find tests/example-synthetic.log --value 38.4 --tol 0.1 --at 18:20:01 --mdi
# expect exactly: 05/0x87  dbid:01  @0  ct_temp_le  38.438
```
