# Wiring the tap

Hardware: **Waveshare ESP32-S3-RS485-CAN**, chosen over the M5Stack ATOM +
ATOMIC RS485 base for its screw terminals. It also happens to be the better
electrical choice here: its RS485 side is galvanically isolated and carries TVS
/ surge / ESD protection, which is worth having on a bus that shares a chassis
with a compressor contactor.

## Read this first: the 24 V is AC

ComfortNet runs four conductors between equipment: **Data 1**, **Data 2**, **R**
and **C**. R/C is **24 V AC**, not DC.

The Waveshare board's screw-terminal input accepts **7–36 V DC only**. Putting
24 V AC into it will damage it.

Power the board one of these ways instead:

- **USB-C, 5 V** — simplest. A phone charger inside the air handler cabinet, or
  a run out to a nearby outlet.
- **24 V AC → 12 V DC converter**, then into the DC screw terminals. Use this
  if you want the monitor to power-cycle with the equipment.

Do not attempt to rectify R/C with a couple of diodes and hope. The bus common
is shared with the equipment's low-voltage side; a sloppy supply here couples
noise straight into the differential pair you are trying to read.

## Bus connections

| ComfortNet | RS485 | Waveshare terminal |
|---|---|---|
| Data 1 | non-inverting | `A+` |
| Data 2 | inverting | `B−` |
| C | common | **not connected** — the board has no RS485 ground terminal |
| R | 24 V AC | **not connected** |

Only the two data lines connect. The board's RS485 side is galvanically
isolated and does not bring its isolated ground out to a terminal, so there is
nothing to land C on. That is fine for a two-wire tap: with no DC path to any
other ground, the isolated side floats, and the receiver's own input network
holds it near the bus's common-mode voltage.

Do **not** substitute the GND on the board's power terminals. That is the
non-isolated side: tying it to C bypasses the isolation, and once the board is
on a DIN-rail supply it can create the ground loop the isolation exists to
prevent.

Polarity: Data 1 → `A+` follows esphome-comfortnet's mapping and the usual
idle bias (Data 1 sits a few tenths of a volt above Data 2, relative to C). To
confirm with a meter, measure 1–C and 2–C in DC volts on a live, idle bus; the
higher one goes to `A+`. A swap is harmless and shows up in the log as checksum
mismatches or no valid frames.

If captures later show intermittent checksum errors that coincide with the
compressor or blower starting, common-mode noise is a suspect. Only then is a
ground reference worth revisiting, and only through an isolated-side ground.

### Splicing

Use one 3-port lever nut per data line rather than landing a third conductor
under an air handler terminal screw: move the thermostat conductor off the
terminal, put a short pigtail back in its place, and join pigtail, thermostat
wire and monitor lead in the lever nut. No terminal gains a conductor, polarity
can be swapped without touching a screw, and the monitor can be removed without
disturbing the bus. Keep the monitor leads short and twisted, keep the lever
nuts in the low-voltage area away from condensation and the blower, and label
them 1 and 2.

## Termination

The board has a **120 Ω termination resistor, disabled by default**, enabled by
a jumper.

**Leave it disabled.** You are tapping into the middle of an existing bus that
is already terminated at its two physical ends. Adding a third termination
loads the bus and can cause exactly the intermittent framing errors you will
then waste a weekend chasing.

Only enable it if you have determined you are electrically at one end of the
segment and that end is currently unterminated.

## Where to tap

Tap on the **equipment side**: the segment that joins the air handler and the
outdoor unit. If a zone controller such as the EWC UT-3000 is installed, tap
between it and the equipment, not on the thermostat side.

```
     [thermostat]         [thermostat]
           |                  |
           +--------+---------+
                    |
           [zone controller]          <-- if installed, e.g. EWC UT-3000
                    |
        =========== CT-485 ===========   <-- TAP HERE
             |                |
     [DFVE36CP1300]    [DH7VSA2410A]
      air handler       outdoor unit
```

Physically, the easiest points are usually:

- the RJ-11 diagnostic port on the air handler control board, if it is fully
  wired (some boards do not populate all pins), or
- the 1 / 2 / R / C terminal block, tapping alongside the existing conductors.

Diagnostic RJ-11 pinout, front view, from the CT-485 physical spec:

```
             ┌─────────┐
          1 ─┼──       │
C (24VAC) 2 ─┼──       └──┐
2     (B) 3 ─┼──          │
1     (A) 4 ─┼──          │
R   (GND) 5 ─┼──       ┌──┘
          6 ─┼──       │
             └─────────┘
```

Note the labelling trap: on this connector **pin 5 is R** and **pin 2 is C**,
and esphome-comfortnet's diagram annotates R as the one you treat as ground
reference. Meter it before you trust it — verify which pair actually carries
the differential signal by looking for ~2.5 V common mode and activity, rather
than assuming from the diagram.

## Before you power on

The single most important software precaution:

**`flow_control_pin: GPIO21` must be set.** On this board the RS485
driver-enable is under GPIO control, not automatic. An unconfigured GPIO21
floats, and a floating driver-enable can assert the transmitter and jam the bus
that is currently running your heating.
`firmware/waveshare-esp32-s3-rs485-can.yaml` sets it; do not remove it, and do not port this config to another board without
checking that board's direction control.

## Safety

- Kill power at the disconnect before opening the air handler cabinet. The
  low-voltage section is 24 V, but line voltage is inches away.
- The outdoor unit holds a charged DC bus after power-off. Do not open it.
- The monitor is **listen-only** until you turn Bus Transmit on, but a device
  on the bus can still perturb it. If the system starts behaving oddly, unplug the monitor
  first and see whether the behaviour persists.
