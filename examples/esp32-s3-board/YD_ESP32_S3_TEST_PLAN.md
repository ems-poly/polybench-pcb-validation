---
plan_format: 1
living: true
project: yd-esp32-s3
type: plan
status: draft
---

# YD-ESP32-S3 (N16R8) test plan

This is the master for the unpowered and powered steps of the YD-ESP32-S3 variant of the public Polybench demo. Designators, pad names (`REF.PIN`) and nets are those of the KiCad redraw in `kicad/yd-esp32-s3/` (from the maker's SCH V1.4, see its `PROVENANCE.md`), so the ECAD plug-in highlights them in the open board. The DevKitC-1 variant keeps `ESP32_S3_BOARD_TEST_PLAN.md`; this file does not change it. All limits are `guess` or `record`: nothing here has been closed against a datasheet page.

## 1. How it works

Tier 1 runs on every board. Tier 2 is a short deeper review that the decision gate sends a board to when a Tier 1 reading is out of band. Test ids are never reused: U01, U02 and P03 of the shared plan are retired in this file (their pins and meaning changed with the schematic) and the next free numbers are taken.

What the schematic says about the pins used here (SCH V1.4): J1 pins 1 and 2 are both 3V3 (net VDD33) and J1 pin 22 is GND. J1 pin 21 is the **5V header pin**: it sits on the anode side of diode D3 (net 5V_HDR) and reaches the 5V rail only through the solder jumper IN-OUT, which is in parallel with D3. With IN-OUT open, USB power does not appear on J1 pin 21; the pin is an input through D3 only (derivation in `PROVENANCE.md`).

**Bench facts (stated by the operator on 10 Nov 2026, not measured).** On the operator's boards no solder jumper is bridged: RGB is open and IN-OUT is open. So GPIO48 does not reach the RGB LED on these boards and no test here expects the LED to light, and the 5V header pin is not driven by USB power. The jumper states are still recorded per board (U06, U07), because a board that differs changes what applies. Ports, looking at the USB end with the component side up: left = native USB (USB2, OTG), right = COM, the CH343P UART (USB1) (the viewing orientation is an interpretation, to be confirmed by the operator).

## Tier 1 — Unpowered

### Rails to ground (J1)

Unplug every cable. Multimeter on resistance. Read after about 5 s; the value drifts upward while the capacitors charge. J1 is the header row that carries 3V3, RST and 5V (pin 1 is the square pad at the antenna end).

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U04 | 5V header pin (J1.21) to GND (J1.22), resistance | J1.21 | J1.22 | 5V_HDR, GND | J1.21, J1.22 | number | kOhm | 1.0 | — | guess | closes on: the readings of the first three boards. J1.21 is the anode side of D3: the meter reads through D3 into the 5V rail; IN-OUT is open on the operator's boards (stated) | below 0.1: 5V looks shorted: do not plug this board in. | x0.5..x2 | keyboard, voice |
| U05 | 3V3 pin (J1.1) to GND (J1.22), resistance | J1.1 | J1.22 | VDD33, GND | J1.1, J1.22 | number | kOhm | 0.5 | — | guess | closes on: the readings of the first three boards | below 0.1: 3V3 looks shorted: do not plug this board in. | x0.5..x2 | keyboard, voice |
| U03 | Multimeter model and range | — | — | — | — | text | — | — | — | record | — | — | — | keyboard |

- U04: Red lead on J1 pin 21 (labelled 5V), black lead on J1 pin 22 (labelled GND).
- U05: Red lead on J1 pin 1 (labelled 3V3, square pad), black lead on J1 pin 22.

### Jumper state (IN-OUT, RGB)

Look at the two solder-jumper pads next to the labels IN-OUT and RGB (top side, near the RGB LED). A pad pair with solder across it is bridged; bare pads are open. This decides whether the 5V header pin follows the USB rail (IN-OUT) and whether GPIO48 reaches the RGB LED (RGB). The operator states that both are open on his boards (10 Nov 2026); record what you see anyway. The serial LED question (F12, Program and function) is only an expected Yes when U07 is bridged.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U06 | IN-OUT jumper state (options: bridged / open / unsure) | — | — | 5V_HDR, 5V | IN-OUT | choice | — | — | — | record | — | — | — | keyboard |
| U07 | RGB jumper state (options: bridged / open / unsure) | — | — | GPIO48, RGB_CTRL | RGB, U1 | choice | — | — | — | record | — | — | — | keyboard |

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

### Power on (U3, U4, USB1, PWR)

Plug the board in by its USB-UART port (USB1, marked COM: the right-hand port looking at the USB end with the component side up, which is the USB-C connector beside the CH343P chip U2 on the J2 header side; see the README) through a USB power meter that shows volts and milliamps. Read the current after 10 s. Unplug at once at any smell or heat.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P01 | USB current at idle, 5 V | — | — | — | U4, U3, USB1 | number | mA | 20 | 150 | guess | closes on: the readings of the first three boards and the module datasheet current table | above 400: Over-current: unplug now. | ±25% (min 5 mA) | keyboard, voice |
| P02 | 3V3 rail at J1.1 | J1.1 | J1.22 | VDD33, GND | J1.1, J1.22, U3 | number | V | 3.15 | 3.45 | guess | closes on: ESP32-S3-WROOM-1 datasheet v1.8, section 6.2, Table 6-2, p. 27 (supply voltage VDD33 3.0 to 3.6 V; cited from the brief, the PDF was not opened here) | outside 3.0..3.6: 3V3 is outside the module's supply range: unplug. | — | keyboard, voice |
| P06 | 5V header pin J1.21 to GND J1.22, USB-powered, no load, IN-OUT open (U06) | J1.21 | J1.22 | 5V_HDR, GND | J1.21, J1.22, IN-OUT | number | V | — | — | record | — | — | — | keyboard, voice |
| P04 | Power LED PWR lit | — | — | VDD33 | PWR, R6 | yn | — | — | — | guess | closes on: the first board | on No: No power LED: unplug and check 3V3. | — | keyboard |

- P06: Red lead on J1 pin 21, black lead on J1 pin 22, board powered through USB1 with nothing on the headers. Expected from the schematic: **the pin is not driven** (D3's cathode is on the 5V rail, its anode on this pin, IN-OUT open), so the reading is not VBUS minus a diode drop; it is set by D3's leakage into the meter and can be anything from 0 V up to the rail. The schematic does not settle the number, so the test records it with no limit: write the meter model in U03 and note a second meter if you have one. If U06 says bridged, the pin follows the 5V rail instead.

## Tier 2 — Unpowered (deeper review)

### Short hunt (U3, U4, C3, C4, D1)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U20 | Where the low resistance was found, and what was done | — | — | — | U3, U4, C3, C4, D1 | text | — | — | — | record | — | — | — | keyboard |
| U21 | After rework the failing Tier 1 test reads in band | — | — | — | — | yn | — | — | — | guess | closes on: the batch | — | — | keyboard |

## Tier 2 — Powered (deeper review)

### Heat (U3, U4)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P20 | Hottest point of the module shield after 60 s, IR thermometer | — | — | — | U4 | number | degC | — | 60 | guess | closes on: the first three boards | above 80: Too hot: unplug. | — | keyboard |

## Not covered

Antenna performance, USB data integrity, GPIO levels, every header pin other than the 3V3, 5V and GND pins above, and the RGB LED on a board whose RGB jumper is open (the LED cannot light there; F12 only records what was seen).

## Revision history

| Revision | Date | Change | Source |
|---|---|---|---|
| r1 | 2026-11-06 | First issue for the YD variant: designators, pads and nets from the KiCad redraw of SCH V1.4. U01, U02, P03 of the shared plan retired (J3 does not exist on this board; the 5V pin is J1.21 behind D3 and IN-OUT); U04, U05, P05 added; U06, U07 jumper states added | KiCad redraw brief |
| r2 | 2026-11-10 | Operator facts (stated, not measured): no solder jumper bridged, RGB and IN-OUT open; USB port sides. P05 (J1.21 4.6 to 5.25 V, only if IN-OUT bridged) retired, its premise does not hold on these boards; P06 added as a record of J1.21 with IN-OUT open. U06, U07 kept as records. F09 (LED blinks, guess) retired in `function.yaml`, F12 added as a record | Operator statement 10 Nov 2026; derivation in `kicad/yd-esp32-s3/PROVENANCE.md` |
