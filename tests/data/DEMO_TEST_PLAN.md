---
plan_format: 1
living: true
project: demo
type: plan
status: draft
---

# DEMO-1 test plan

Related: [[DEMO_Notes]]

This prose, and every heading that is not a tier section, is ignored by the parser.

## 1. How it works

| Not | A | Test | Table |
|---|---|---|---|
| ignored | because | it is in a prose | section |

## Tier 1 — Unpowered

### Input and rails (J1, C1, C2, C3, U1)

Board unpowered, multimeter on resistance, black lead on the C2 pad. Read after about 5 s.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U01 | 5V rail to GND, ohms | J1 | C2 | 5V, GND | C2, U1.3 | number | kOhm | 1.0 | — | guess | closes on: the ten readings of the first batch | below 0.1: 5V looks shorted: do not power this board. | x0.5..x2 | keyboard, voice |
| U02 | 3V3 rail to GND, ohms | C3 | C2 | 3V3, GND | C3, U1.4 | number | kOhm | 1.0 | — | hard | calc C1 | below 0.05: 3V3 shorted. | x0.5..x2 | — |
| U03 | D1 forward drop, diode mode | D1 | C2 | — | D1 | number | V | 0.3 | 0.9 | hard | DS-DEMOTVS p.4 Table 2 | outside 0.2..1.2: D1 is not a diode. | — | — |
| U04 | Multimeter model and range | — | — | — | — | text | — | — | — | record | — | — | — | keyboard |

- U03: Red lead on the anode, which is the pad away from the band.

### Pull-ups (R3, R4, R5)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U05 | R3 across its pads | R3 | R3 | — | R3 | number | kOhm | 9.8 | 10.2 | hard | calc C2 | — | — | — |
| U06 | R4 across its pads | R4 | R4 | — | R4 | number | kOhm | 9.8 | 10.2 | hard | calc C2 | — | — | — |

## Tier 1 — Powered

### First power (U1, U3, C1)

Actions: read_rails
Supply 12.0 V, current limit 100 mA. Switch off at once at any smell.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P01 | Idle input current at 12 V | — | — | — | U1, U3, C1 | number | mA | 10 | 30 | guess | closes on: the ten readings | above 100: Over the limit: switch off. | ±25% (min 0.5 mA) | keyboard, action |
| P02 | 5V rail | C1 | C2 | 5V, GND | C1, U1.3 | number | V | 4.9 | 5.1 | hard | DS-DEMOREG p.5 Table 3 | above 5.5: 5V is above the MCU limit. Switch off. | — | keyboard, action |
| P03 | 3V3 rail | C3 | C2 | 3V3 | C3 | number | V | 3.27 | 3.33 | hard | calc C5 | — | — | keyboard, action |
| P04 | Idle current after the soak | — | — | — | U1 | number | mA | 10 | 30 | guess | closes on: the ten readings | above 100: Over the limit: switch off. | ±25% (min 0.5 mA) | keyboard, action |
| P05 | Nothing too hot to hold a fingertip on | — | — | — | U1, U3 | yn | — | — | — | guess | closes on: an IR reading on the first board | on No: Too hot: switch off and set the board aside. | — | — |
| P06 | MCU state (options: held in reset (RST to GND) / blank MCU running) | — | — | — | U1 | choice | — | — | — | record | — | — | — | — |

## Decision gate

Gate A after the unpowered readings, gate B after all Tier 1 readings.

```gate
after: Tier 1 Unpowered
to: Tier 2 Unpowered
any out of band
any stop
outlier from 3 boards
hold power
halt when more than 50 % of boards are out of band on one test
```

```gate
after: Tier 1
to: Tier 2
any out of band
any stop
outlier from 3 boards
```

## Tier 2 — Unpowered (deeper review)

### Power diagnosis (U1, U2, U3)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U20 | Where the fault was found and what was done | — | — | — | U1, U2, U3 | text | — | — | — | record | — | — | — | keyboard |
| U21 | After rework the failing Tier 1 test reads in band | — | — | — | — | yn | — | — | — | guess | closes on: the batch | — | — | — |

## Tier 2 — Powered (deeper review)

### Front end (U2, R3)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P20 | Front end input level | R3 | C2 | 3V3 | R3, U2 | number | V | 3.0 | 3.4 | guess | closes on: the ten readings | — | — | — |

## Not covered

Prose.

## Revision history

| Revision | Date | Change | Source |
|---|---|---|---|
| r1 | 2026-10-16 | First issue | author |
