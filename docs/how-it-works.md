# How it works

## The bus

Daikin Fit equipment talks ClimateTalk over CT-485, a two-wire RS-485 bus that
shares the thermostat cable with 24 V AC power. Every node has a node type:

| Node type | Equipment |
|---|---|
| `0x01` | thermostat or zone controller |
| `0x03` | air handler, which is also the network coordinator |
| `0x05` | heat pump (outdoor unit) |
| `0x1D` | gateway: how the monitor identifies itself |

The coordinator assigns addresses. On the tested system the thermostat or zone
controller is `0x01`, the heat pump `0x02`, and the monitor `0x10` or above.
Most messages go to `0xFF` and are routed by node type, so an address change
after a reboot is harmless.

The bus is token-passing: a node transmits only when offered a turn. The
coordinator runs a dataflow cycle about every 16.8 s and offers roughly one
turn per cycle.

## Where the data is

| Kind | Messages | Carries | Requested by |
|---|---|---|---|
| Generic ClimateTalk | Get Status (`0x02` → `0x82`), Get Sensor Data (`0x07` → `0x87`), demand commands (`0x62`–`0x66`) | faults, demands, airflow, air handler refrigerant temperatures and coil pressure | the thermostat or zone controller, routinely |
| Daikin vendor blocks | Get Mfr Generic Data (`0x20` → `0xA0`) | compressor, inverter, power, outdoor temperatures and pressure, EEVs, fan and blower speeds | whoever asks, and only then |

The heat pump answers `0x20` with payload `09.00.02.02.00.C8.FF.00.00` with a
60-byte block, and the air handler answers `09.00.03` with a 9-byte block. A
35-byte heat pump block (operation mode) answers the air handler's own
requests, so it flows without anyone else asking.

## Why the monitor can transmit

Vendor blocks appear only when a node requests them. Measured on the tested
system:

| Stream | Thermostat directly on the bus | Behind an EWC UT-3000 zone controller |
|---|---|---|
| Heat pump 60-byte block | every ~80 s | every ~210–230 s |
| Heat pump 35-byte block | every ~18 s | every ~16 s |
| Air handler 9-byte block | every ~73 s | **never** |

A Daikin One+ connected directly to the equipment requests both blocks about
every 75 s, so a listen-only monitor sees everything. The UT-3000 requests the
heat pump block only every ~3.5 minutes, as ClimateTalk v1, and never the air
handler block. Daikin's cloud then reports those fields as unavailable. To fill
the gap, the monitor can join the network and send the requests itself.

## Listen only and Bus Transmit

The monitor starts **listen only**: it decodes whatever other nodes already
request and writes nothing. The **Bus Transmit** switch in Home Assistant turns
transmitting on and off at runtime:

- **On:** the monitor answers node discovery, joins, and starts polling.
  **Bus Joined** turns on within about 20 s.
- **Off:** every write stops immediately. A request already sent is dropped,
  the coordinator removes the node, and Bus Joined turns off (33 s measured).
  Passive decoding continues.

`bus_transmit_at_boot` in the device config sets the state at power-up:
`ALWAYS_OFF` (default), `RESTORE_DEFAULT_OFF` or `ALWAYS_ON`.

When transmitting, the monitor sends only:

- the housekeeping a member node must answer: discovery and address responses,
  token offer replies, acknowledgements, node list and node ID responses, and
  network shared data storage
- Get Mfr Generic Data (`0x20`) requests to the heat pump and the air handler,
  framed as the One+ sends them (destination `0xFF`, route by node type), each
  at most every 60 s
- Get Sensor Data (`0x07`) to the air handler, at most every 20 s, for coil
  pressure and refrigerant line temperatures. A zone controller alone requests
  it about every 2 minutes.

It sends no control commands.

## Changes to esphome-comfortnet

The component in `components/comfortnet` is
[esphome-comfortnet](https://github.com/esphome-comfortnet/esphome-comfortnet)
with these changes:

- **Polling with request payloads**, which the vendor blocks need. Upstream
  could only poll the four argument-less generic requests.
- **Per-poll repeat intervals** (`poll_interval`). Upstream polled on every
  opportunity to transmit.
- **Request timeout.** A request with no response is dropped after three
  transmissions instead of being resent indefinitely.
- **Join retry.** If no address arrives within 30 s of answering discovery, the
  monitor answers the next discovery. Upstream waited forever.
- **Listen-only mode and a runtime transmit switch.**
- **Command acknowledgements ignored.** 17-byte dataflow acknowledgements were
  being parsed as command `0x0006`.
- **Compile fix for current ESPHome** (a `MAC_ADDRESS_SIZE` macro collided with
  ESPHome's own constant) and initialisation of members left indeterminate.

## Bus load

Measured on the tested system with the monitor joined behind the UT-3000:

| Check | Result |
|---|---|
| Monitor requests answered | all of them in every validation capture |
| Response time | p50 0.9–1.3 s |
| Bus traffic | 110–112 frames/min, against 94–101 without the monitor |
| Monitor share of frames | 24 % with the 20 s sensor data poll |
| Zone controller requests answered | 27 of 27 in the 8-minute capture |
| Timeouts, checksum errors, rejections | none |

In a paired comparison of the same cooling call, passive and joined, the zone
controller's demand pattern showed no sign of changing because of the monitor.
Weather and house load differed between the two runs, so that is not proof
either way.

## Poll cadence

The bus sets the ceiling. A node gets at most one turn per ~16.8 s dataflow
cycle, and each vendor request is an exchange of about eight frames taking
1–1.3 s at 9600 baud.

| Vendor block interval | What happens | Risk |
|---|---|---|
| 60 s (default) | each request about once a minute, like a One+ on its own | low |
| 30 s | about every other cycle; roughly 30 % more frames than idle | low to moderate |
| 10 s or less | not achievable: requests are always due, so the monitor claims a turn every cycle | moderate to high |

Claiming a turn every cycle competes with the thermostat's or zone controller's
commands and the air handler's own requests, and communicating equipment can
raise faults when exchanges are missed. About 17 s is the practical floor for
the sensor data poll; its real repeat interval at 20 s is ~27 s, because a
request waits for the next turn. Back off if request timeouts or missed
acknowledgements appear in the log.

## Known behaviour

- **The address can change across reboots,** for example from `0x10` to
  `0x11`. The coordinator still holds the previous session for about a minute,
  and a rebooted monitor answers discovery with a new session ID.
- **The EWC UT-3000 answers node discovery from address `0x00`** (MAC field
  `CT300`) about every 2 minutes while operating as `0x01`. If its answer lands
  in the same cycle as the monitor's, the monitor is not given an address and
  retries 30 s later. **Bus Unjoined Discovery Responses** counts these
  answers.
- **The monitor occasionally drops off the bus** for up to about 5 minutes and
  rejoins by itself. The cause is not known yet.
