# ESPHome Daikin Fit

An ESPHome device that reads Daikin Fit heat pump and air handler telemetry
from the local ClimateTalk / CT-485 bus and publishes it to Home Assistant:
compressor speed and current, inverter, power, refrigerant temperatures and
pressures, EEV openings, fan and blower speeds, airflow, and the demand passed
between the units. No cloud connection is needed.

> **Not affiliated with Daikin.** Daikin, Daikin Fit and Daikin One are
> trademarks of their owners. This is an independent project, built by
> observing a single installed system. Connecting anything to your HVAC
> communication bus is at your own risk; read [`docs/wiring.md`](docs/wiring.md)
> first.

## Tested hardware

Every decoded field was verified on one system:

| Part | Model |
|---|---|
| Outdoor unit | Daikin Fit heat pump DH7VSA2410A (2-ton, R-32) |
| Air handler | DFVE36CP1300 |
| Thermostats | Daikin One+ and Daikin One Touch |
| Zone controller | EWC UT-3000 (also tested without it) |
| Monitor | Waveshare ESP32-S3-RS485-CAN |

**Untested, may work:** other Daikin Fit heat pump sizes and generations
(DH6VS, DH7VS, DH9VS), and other DFVE/DMVE air handlers. The DX6VS service
instructions show the same monitoring items and operation codes as the DH7VSA,
which suggests a shared outdoor control platform, but byte positions in the
vendor data blocks may differ between models.

**Likely partial:** Daikin Fit air conditioners (DX6VS, DX9VS) have no heating,
defrost or reversing data, so those sensors may stay empty or read wrongly.
Fit systems with a communicating furnace instead of a DFVE air handler use a
different indoor status block; the air handler decode will not apply.

Reports from other equipment are welcome: open an issue with the outdoor unit
and indoor unit model numbers. [`docs/decoding-other-models.md`](docs/decoding-other-models.md)
explains how to check the decode on your system.

## How it works

The monitor taps the two data wires between the air handler and the outdoor
unit. It starts **listen only**: it decodes what the thermostat already
requests and writes nothing to the bus. A thermostat connected directly to the
equipment requests everything, so listening is enough.

A zone controller such as the EWC UT-3000 requests the Daikin vendor data
rarely or never. Turn on the **Bus Transmit** switch in Home Assistant and the
monitor joins the network and requests it itself, about once a minute. It sends
no control commands. Details are in [`docs/how-it-works.md`](docs/how-it-works.md).

## Quick start

1. Read [`docs/wiring.md`](docs/wiring.md). There are two ways to damage
   hardware here: 24 V **AC** into a DC-only input, and an unconfigured RS485
   driver-enable jamming a live bus.
2. Copy `firmware/secrets.yaml.example` to `firmware/secrets.yaml` and fill it
   in.
3. Build and flash from a full checkout of this repository, with a recent
   ESPHome (tested with 2026.8.2; sub-devices need 2025.7 or later):

   ```bash
   esphome run firmware/waveshare-esp32-s3-rs485-can.yaml
   ```

4. Adopt the device in Home Assistant. If a zone controller sits between your
   thermostats and the equipment, turn on **Bus Transmit**.

For a different ESP32 board, copy the device config and change the board, the
UART pins and the RS485 direction pin (`flow_control_pin`).

## Home Assistant

Entities appear on four devices: **Heat Pump** (27), **Air Handler** (11),
**Zone Controller** (4: the thermostat's or zone controller's demand
commands), and the monitor itself (6). Only values carried on the bus are
published; nothing is calculated in the firmware.
[`docs/decoded-fields.md`](docs/decoded-fields.md) lists every entity with its
bus source, encoding and how confident the decode is.

## Layout

```
components/comfortnet/          ClimateTalk component (modified esphome-comfortnet)
firmware/
  daikin-fit.yaml               decode package: fields and entities
  waveshare-esp32-s3-rs485-can.yaml   device config
  secrets.yaml.example
docs/
  wiring.md                     tapping the bus safely
  how-it-works.md               the bus, listen-only and transmit, bus load
  decoded-fields.md             every entity, its source and confidence
  decoding-other-models.md      checking the decode on other equipment
tools/                          capture and decoding tools (standard library only)
tests/                          synthetic capture for checking the tools
```

## Status

Decoded and running on the tested system. Open items:

- operation mode code meanings, and codes for defrost
- the inverter fin temperature unit (decoded as °C + 40, inferred)
- three sensors marked "(Unverified)": dehumidification demand on both sides,
  and defrost demand
- the monitor occasionally drops off the bus for a few minutes and rejoins

## License

GPL-3.0, see [`LICENSE`](LICENSE). `components/comfortnet` is derived from
esphome-comfortnet, which is also GPL-3.0; the changes are listed in
[`docs/how-it-works.md`](docs/how-it-works.md).

## Credits

- [esphome-comfortnet](https://github.com/esphome-comfortnet/esphome-comfortnet)
  — the ClimateTalk component this builds on
- [zlangbert/ha-daikinone](https://github.com/zlangbert/ha-daikinone) — the
  cloud field catalog used to decode the bus
- [rrmayer's ClimateTalk spec archive](https://github.com/rrmayer/climate-talk-web-api/tree/master/docs/spec)
- [kpishere/Net485](https://github.com/kpishere/Net485) — CT-485 reference
  implementation
