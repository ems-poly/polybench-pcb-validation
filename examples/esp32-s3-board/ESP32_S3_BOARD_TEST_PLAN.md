---
plan_format: 1
living: true
project: esp32-s3-board
type: plan
status: draft
---

# ESP32-S3 dev board test plan (YD-ESP32-S3 or ESP32-S3-DevKitC-1)

This is the master for the unpowered and powered steps of the public Polybench demo. The visual checklist and the Program and function step live in the protocol fragments, because format 1 does not generate them. All limits are `guess` or `record`: nothing here has been closed against a datasheet page, and the plan does not pretend otherwise.

## 1. How it works

Tier 1 runs on every board. Tier 2 is a short deeper review that the decision gate sends a board to when a Tier 1 reading is out of band. Designators are the demo labels of BOM (assembly.yaml, assembly_devkitc-1.yaml), not the schematic's.

## Tier 1 — Unpowered

### Rails to ground (J1, J3)

Unplug every cable. Multimeter on resistance. The headers J1 and J3 carry the pins labelled 3V3, 5V and G. Read after about 5 s; the value drifts upward while the capacitors charge.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U01 | 5V pin to G, resistance | J3 | J3 | 5V, GND | J3 | number | kOhm | 1.0 | — | guess | closes on: the readings of the first three boards | below 0.1: 5V looks shorted: do not plug this board in. | x0.5..x2 | keyboard, voice |
| U02 | 3V3 pin to G, resistance | J1 | J1 | 3V3, GND | J1 | number | kOhm | 0.5 | — | guess | closes on: the readings of the first three boards | below 0.1: 3V3 looks shorted: do not plug this board in. | x0.5..x2 | keyboard, voice |
| U03 | Multimeter model and range | — | — | — | — | text | — | — | — | record | — | — | — | keyboard |

- U01: Red lead on the pin labelled 5V of J3, black lead on a pin labelled G.
- U02: Red lead on a pin labelled 3V3 of J1, black lead on a pin labelled G.

## Decision gate

One gate after the unpowered readings, so a suspect board is looked at before it is powered.

```gate
after: Tier 1 Unpowered
to: Tier 2 Unpowered
any out of band
any stop
outlier from 3 boards
hold power
```

## Tier 1 — Powered

### Power on (U1, U2, U3, LED2)

Plug the board in by its USB-UART port (the one through the USB-serial bridge, see the README) through a USB power meter that shows volts and milliamps. Read the current after 10 s. Unplug at once at any smell or heat.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P01 | USB current at idle, 5 V | — | — | — | U1, U3 | number | mA | 20 | 150 | guess | closes on: the readings of the first three boards and the module datasheet current table | above 400: Over-current: unplug now. | ±25% (min 5 mA) | keyboard, voice |
| P02 | 3V3 rail at J1 | J1 | J1 | 3V3, GND | J1, U3 | number | V | 3.15 | 3.45 | guess | closes on: ESP32-S3-WROOM-1 datasheet v1.8, section 6.2, Table 6-2, p. 27 (supply voltage VDD33 3.0 to 3.6 V; cited from the brief, the PDF was not opened here) | outside 3.0..3.6: 3V3 is outside the module's supply range: unplug. | — | keyboard, voice |
| P03 | 5V pin at J3 | J3 | J3 | 5V, GND | J3 | number | V | 4.6 | 5.25 | guess | closes on: the USB power meter's own accuracy | — | — | keyboard, voice |
| P04 | Power LED lit | — | — | — | LED2 | yn | — | — | — | guess | closes on: the first board | on No: No power LED: unplug and check 3V3. | — | keyboard |

## Tier 2 — Unpowered (deeper review)

### Short hunt (U1, U3)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U20 | Where the low resistance was found, and what was done | — | — | — | U1, U3 | text | — | — | — | record | — | — | — | keyboard |
| U21 | After rework the failing Tier 1 test reads in band | — | — | — | — | yn | — | — | — | guess | closes on: the batch | — | — | keyboard |

## Tier 2 — Powered (deeper review)

### Heat (U1, U3)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P20 | Hottest point of the module shield after 60 s, IR thermometer | — | — | — | U1 | number | degC | — | 60 | guess | closes on: the first three boards | above 80: Too hot: unplug. | — | keyboard |

## Not covered

Antenna performance, USB data integrity, GPIO levels and every header pin other than the three rails.

## Revision history

| Revision | Date | Change | Source |
|---|---|---|---|
| r1 | 2026-10-17 | First issue for the public demo | demo brief |
| r2 | 2026-10-17 | P02 stop rule is the module supply range 3.0 to 3.6 V with its datasheet page; USB port wording | gate review D9, D7 |
