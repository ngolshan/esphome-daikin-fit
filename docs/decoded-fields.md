# Decoded fields

Every Home Assistant entity, where it comes from on the bus, how it is encoded,
and how sure the decode is. All of it was worked out on one system: a
DH7VSA2410A heat pump and a DFVE36CP1300 air handler.

## Conventions

**Sources** are written `<node type>/<message>`: `05` is the heat pump, `03`
the air handler. `0xA0` is a Get Mfr Generic Data response (60-, 35- or 9-byte
layout), `0x82` a status response and `0x87` a sensor data response. Status and
sensor data carry blocks identified by a DBID tag; byte offsets count from the
start of the block.

**Encodings:**

- **Temperature word:** u16 little-endian. Bit 15 = valid, bit 14 = negative,
  bits 13–4 = whole °F, bits 3–0 = sixteenths.
- **Half-percent:** u8 ÷ 2, so 200 = 100 %.

**Confidence:**

- **Verified (n %):** tracked the matching Daikin cloud field (named in the
  table) as a time series. n is the share of 36 snapshots, taken 60 s apart,
  that matched at the best lag. The cloud refreshes unevenly, so 94–100 % is
  what a real field scores.
- **Physics:** the cloud field exists, but its uneven sampling prevented a
  clean time match; the value obeys the physical relationship it should.
- **Observed:** behaves as expected on the bus, with no cloud field to compare.
- **Inferred:** the byte is right, but its unit or meaning is deduced. See the
  notes.
- **Unverified:** position taken from the ClimateTalk layout or upstream; never
  seen non-zero.

**Names** follow `<Equipment> [Requested] <Part or line> <Quantity> [Received]`.
Zone Controller, Heat Pump and Air Handler are separate Home Assistant devices.
"Requested" is a value one unit asks of another, named after the part asked;
"Received" is demand a unit reports having been given. Refrigerant names give
the physical point, never its role in one mode: compressor suction and
discharge are always the low and high side, and the liquid line (small pipe)
and vapor line (large pipe) join the two units.

Only values carried on the bus are published. Nothing is computed in the
firmware from other readings.

## Heat Pump

| Entity | Source | Encoding | Confidence | Cloud field |
|---|---|---|---|---|
| Operation Mode Code | `05/0xA0` 35-byte, byte 3 | u8 code | Verified (97 %); meanings undocumented | `ctOutdoorOperationMode` |
| Cool Demand Received | `05/0x82` DBID 0x00, byte 3 | half-percent | Verified (100 %) | `ctOutdoorCoolRequestedDemand` |
| Heat Demand Received | `05/0x82` DBID 0x00, byte 2 | half-percent | Observed (tracked heat demand in the heating check) | `ctOutdoorHeatRequestedDemand` |
| Dehumidification Demand Received | `05/0x82` DBID 0x00, byte 4 | half-percent | Verified (97 % of 88 snapshots) | `ctOutdoorDeHumidificationRequestedDemand` |
| Defrost Demand Received (Unverified) | `05/0x82` DBID 0x00, byte 7 | half-percent | Unverified | — |
| Critical Fault Code, Minor Fault Code | `05/0x82` DBID 0x00, bytes 0, 1 | u8 | Observed (0 throughout) | `ctOutdoorCriticalFault`, `ctOutdoorMinorFault` |
| Requested Air Handler Fan | fan command `0x66` sent by the heat pump, byte 2 | half-percent | Verified (values match) | `ctOutdoorFanRequestedDemandPercentage` |
| Requested Air Handler Airflow | `05/0xA0` 60-byte, bytes 43–44 | u16 LE, CFM | Verified (97 %) | `ctOutdoorRequestedIndoorAirflow` |
| Requested Compressor Frequency | `05/0xA0` 60-byte, byte 3 | half-percent | Verified (97 %) | `ctOutdoorFrequencyInPercent` |
| Compressor Speed | `05/0xA0` 60-byte, byte 14 | u8, rps | Verified (97 %) | `ctCurrentCompressorRPS` |
| Compressor Target Speed | `05/0xA0` 60-byte, byte 20 | u8, rps | Verified (97 %) | `ctTargetCompressorspeed` |
| Compressor Current | `05/0xA0` 60-byte, byte 15 | u8 ÷ 10, A | Verified (97 %) | `ctCompressorCurrent` |
| Compressor Runtime | `05/0xA0` 60-byte, bytes 40–41 | u16 LE, hours | Inferred (constant in the run, later stepped 632 → 633) | `ctOutdoorCompressorRunTime` |
| Inverter Current | `05/0xA0` 60-byte, byte 18 | u8 ÷ 10, A | Verified (97 %) | `ctInverterCurrent` |
| Inverter Fin Temperature | `05/0xA0` 60-byte, byte 19 | u8, °C + 40 | Byte verified (100 %); encoding inferred | `ctInverterFinTemp` |
| Power | `05/0xA0` 60-byte, byte 8 | u8 × 10, W | Verified (97 %) | `ctOutdoorPower` |
| Fan Speed | `05/0xA0` 60-byte, byte 27 | u8 × 10, rpm | Verified (100 %) | `ctOutdoorFanRPM` |
| Fan Target Speed | `05/0xA0` 60-byte, byte 22 | u8 × 10, rpm | Verified (100 %) | `ctTargetODFanRPM` |
| EEV Opening | `05/0xA0` 60-byte, byte 23 | half-percent | Inferred | `ctOutdoorEEVOpening` |
| Outdoor Air Temperature | `05/0xA0` 60-byte, bytes 25–26 | temperature word | Verified (97 %) | `ctOutdoorAirTemperature` |
| Compressor Discharge Temperature | `05/0xA0` 60-byte, bytes 28–29 | temperature word | Verified (97 %) | `ctOutdoorDischargeTemperature` |
| Coil Temperature | `05/0xA0` 60-byte, bytes 30–31 | temperature word | Verified (97 %) | `ctOutdoorCoilTemperature` |
| Defrost Sensor Temperature | `05/0xA0` 60-byte, bytes 32–33 | temperature word | Verified (97 %) | `ctOutdoorDefrostSensorTemperature` |
| Liquid Line Temperature | `05/0xA0` 60-byte, bytes 34–35 | temperature word | Verified (97 %) | `ctOutdoorLiquidTemperature` |
| Compressor Suction Pressure | `05/0xA0` 60-byte, byte 36 | u8, psig | Verified (97 %) | `ctOutdoorSuctionPressure` |
| Compressor Suction Temperature | `05/0xA0` 60-byte, bytes 38–39 | temperature word | Verified (97 %) | `ctOutdoorSuctionTemperature` |

## Air Handler

| Entity | Source | Encoding | Confidence | Cloud field |
|---|---|---|---|---|
| Airflow | `03/0x82` DBID 0x00, bytes 12–13 | u16 LE, CFM | Verified (94 %) | `ctAHCurrentIndoorAirflow` |
| Blower Speed | `03/0xA0` 9-byte, bytes 3–4 | u16 LE, rpm | Verified (94 %) | `ctAHFanMotorRPM` |
| Fan Demand Current | `03/0x82` DBID 0x00, byte 4 | half-percent | Verified (94 %); see notes | `ctAHFanCurrentDemandStatus` |
| Fan Demand Received | `03/0x82` DBID 0x00, byte 15 | half-percent | Verified (94 %); see notes | `ctAHFanRequestedDemand` |
| EEV Opening | `03/0xA0` 9-byte, byte 5 | half-percent | Verified (94 %) | `ctAHEEVOpenRate` |
| Power | `05/0xA0` 60-byte, bytes 10–11 (sent by the heat pump) | u16 LE ÷ 10, W | Verified (97 %) | `ctIndoorPower` |
| Coil Pressure | `03/0x87` DBID 0x02, bytes 12–13 | bit 15 valid, low 12 bits psig | Verified (97 %) | `ctAHPressureSensor` |
| Liquid Line Temperature | `03/0x87` DBID 0x02, bytes 0–1 | temperature word | Verified (97 %) | `ctAHLiquidTemperature` |
| Vapor Line Temperature | `03/0x87` DBID 0x02, bytes 2–3 | temperature word | Verified (97 %) | `ctAHSuctionTemperature` |
| Vapor Line Superheat | `03/0x82` DBID 0x01, bytes 9–10 | temperature word | Physics | `ctAHSuperHeatValue` |
| Liquid Line Subcooling | `03/0x82` DBID 0x01, bytes 39–40 | temperature word | Physics | `ctAHSubCoolValue` |

## Zone Controller

Demand commands from whichever node sends them at address `0x01`: the zone
controller, or a thermostat connected directly to the equipment.

| Entity | Source | Encoding | Confidence |
|---|---|---|---|
| Cool Demand | command `0x65`, byte 1 | half-percent | Observed (matches Heat Pump Cool Demand Received) |
| Heat Demand | command `0x64`, byte 1 | half-percent | Observed (heating check) |
| Fan Demand | command `0x66` with byte 1 = `0x00`, byte 2 | half-percent | Observed |
| Dehumidification Demand | command `0x62`, byte 1 | half-percent | Observed (matches the thermostat's `ctControlAlgorithmDehumDemand`) |

## Monitor

| Entity | What it is |
|---|---|
| Bus Transmit | switch: listen only (off) or join and poll (on) |
| Bus Joined | whether the monitor holds a network address |
| Bus Unjoined Discovery Responses | count of discovery answers from nodes without an address |
| Monitor Uptime, Monitor WiFi Signal Strength, Monitor Restart | device housekeeping |

## Notes

### Fan commands have two senders

Fan command `0x66` comes from the thermostat or zone controller *and* from the
heat pump, told apart by payload byte 1:

| Payload | Sender | Meaning |
|---|---|---|
| `60.00.nn` (zone controller), `a0.00.nn` (thermostat) | zone controller or thermostat | fan demand |
| `a0.01.nn`, `00.01.00` | heat pump, cooling | airflow request to the air handler |
| `a0.02.nn`, `00.02.00` | heat pump, heating | the same, when heating |

The firmware treats byte 1 = `0x00` as the thermostat or zone controller and
anything else as the heat pump. The heat pump keeps requesting fan while it
winds down after a call ends, so mixing the two makes an idle system look
"fan only".

### Operation Mode Code

What the numbers mean is not documented, so only the number is published.
Values seen: 0–9 and 12.

- Daikin's DH7VSA installation manual (Monitor Mode item 2, "Operation code")
  lists 0 Stop, 1 Cooling start-up, 2 Heating start-up, 3 Oil return,
  4 Heating, 5 Defrost, 6 Cooling. That table does **not** describe this byte:
  the cloud pairs this byte's 5 with "Cooling", and the byte reaches 7, 8, 9
  and 12.
- Cloud snapshots reporting both this number and the `ctOutdoorMode` text
  paired 0, 2 and 4 with "Stop", and 5 and 6 with "Cooling".
- Sequences seen (not meanings): cooling ran 0 → 2 → 4 → 5, with 6 while the
  compressor ramped and 7 → 1 → 0 at the end. Heating ran 0 → 3 → 8 → 9,
  ending 12 → 1 → 0.

### Inverter Fin Temperature

The byte tracks `ctInverterFinTemp` exactly; the unit is the open question. It
is decoded as °C + 40 because:

- **Settled idle** (compressor off and ≤ 10 W for at least an hour, mostly
  overnight): the byte read 52–61 with outdoor air at 14–23 °C. Read as plain
  °C, the heatsink would sit a steady +39 °C above ambient (median; +36 to +46)
  on about 10 W, following outdoor air at 0.88 °C per °C.
- **Under load,** the same comparison rose only to +42 °C at 400–800 W and
  +51 °C above 1.2 kW. A heatsink that sheds 1.2 kW of inverter losses with
  12 °C of extra rise cannot sit 39 °C above ambient on 10 W.
- Read as °C + 40, idle sits about 1 °C below outdoor air (a casing cooling
  overnight) and rises with load.
- Daikin's fin overheat fault trips at 95 °C (E32 in the DX6VS service
  instructions) and assumes outdoor air up to 46 °C. As plain °C, the unit
  would idle near 85 °C on such a day.

No public document gives the encoding: the service instructions read fin
temperature only with Daikin's D-checker tool. ha-daikinone reads the cloud
field as plain °C, but cites no source. Measuring the heatsink with an IR
thermometer after an hour idle, or checking it at 0 °C outdoor air, would
settle it.

### Refrigerant readings

- **Which superheat or subcooling to read:** Air Handler Vapor Line Superheat
  when cooling (the indoor coil is the evaporator), Air Handler Liquid Line
  Subcooling when heating (the indoor coil is the condenser). The other reads
  large, negative or near zero in that mode.
- **Air Handler Liquid Line Temperature when heating** read 65–69 °F with the
  coil condensing at 100–103 °F, colder than the return air. The air handler's
  own subcooling agrees (33–36 °F), so the decode matches the air handler, but
  the reading does not fit a coil outlet. Treat heating subcooling as
  unreliable.
- **Coil pressure above 255 psig** decodes correctly: it reached 339 psig in
  the heating check. Compressor suction pressure fell when heating, confirming
  it is the low side in both modes.
- **EEV openings** are half-percent: Air Handler EEV Opening reads 100 %
  (byte 200) whenever heating.
- **Heat Pump EEV Opening** is a candidate. The cloud value only took 0 and 68
  in the decode run, and byte 23 is the only unassigned byte that read 0 at
  idle and a constant 68 % when running. It varied 7–31 % when heating, which
  fits an outdoor EEV; there was no cloud value to confirm against.

### Requested frequency and target speed

Requested Compressor Frequency mostly follows the demand the heat pump
received. Compressor Target Speed is the outdoor controller's own setpoint: it
rises with the request (100 % lined up with the highest speed seen, 78 rps),
never went below 19 rps while running, steps through start-up and wind-down
sequences, and stays up for a while after the request drops to 0. Compressor
Speed follows the target within about 1 rps.

### Air handler fan demand bytes

Fan Demand Current (byte 4) and Fan Demand Received (byte 15) have been
identical in every sample, so the cloud field match cannot tell which is which.
Both show the fan command the air handler is following, from the heat pump
during calls and from the thermostat or zone controller otherwise.

### Dehumidification

Checked during a dehumidification call on 2026-09-15 (Daikin One+ connected
directly to the equipment, indoor humidity above its 55 % setpoint while
cooling), with a bus capture and a cloud snapshot every 60 s:

- **Zone Controller Dehumidification Demand** (command `0x62`) arrived as
  `A0.C8` (100 %) during the call and `00.00` when it ended, matching the
  thermostat's own dehumidification demand in the cloud
  (`ctControlAlgorithmDehumDemand`: 200, then 0). Behind an EWC UT-3000 it read
  70 %: the thermostat's 100 % scaled by the 70 % zone weight, as with cool
  demand.
- **Heat Pump Dehumidification Demand Received** (status byte 4) matched the
  cloud's `ctOutdoorDeHumidificationRequestedDemand` in 85 of 88 snapshots
  (97 %, 16 distinct values), and every change of the cloud's condensing unit
  dehumidify demand in Home Assistant. While it is non-zero, Cool Demand
  Received reads 0: the heat pump reports the call's demand in one byte or the
  other, never both.
- The cloud reported `ctOutdoorDehumidificationEnable` = 1 throughout; what that
  value means is not documented.

### Zone controller demand after a reboot

A zone controller sends a demand command only when its value changes, so after
the monitor reboots its demands are unknown until the next change. Meanwhile
the monitor sets a demand to 0 when the equipment reports receiving none (Heat
Pump Cool or Heat Demand Received, Air Handler fan demand). It never fills in a
non-zero value.

## Not decoded

- **Not found on the bus:** `ctODFanMotorCurrent` and `ctODCompressorDCVoltage`
  vary in the cloud, but no byte in any stream tracks them.
- **Not identifiable from the runs so far:** `ctCrankCaseHeaterOnOff`,
  `ctPreHeatOnOff`, `ctPreHeatOutput`, `ctReversingValve`,
  `ctOperationRequestToCompressor` and `ctOperationRequestToODFanMotor` changed
  only once each. `ctOutdoorCoolMaxRPS` was constant.
- **Air handler status block 0x00, byte 3** read 0–1 when cooling and 2 when
  heating; it may be the air handler's mode.
- **A second discharge temperature** at byte 26 of the 35-byte block matched
  only 39 % of snapshots; the 60-byte one is used.
- **Command `0x60`, damper closure position demand.** A Daikin One+ sends it
  repeatedly with every value at `C8` (100 %). It is deliberately not decoded
  into an entity: a zone controller takes exclusive control of the dampers, so
  what the thermostat asks for here has no visible effect on a zoned system.

## Open questions

- Operation mode codes for defrost and other heating states.
- Verify Defrost Demand Received (Heat Pump status byte 7): during a
  heating-season defrost it should turn non-zero while Operation Mode Code
  shows a new value. Then drop "(Unverified)" from its name.
- The inverter fin temperature unit.
- Why Air Handler Liquid Line Temperature reads below return air when heating.
- Which air handler fan demand byte is "current" and which "received".
- Whether other Fit models use the same byte positions.
