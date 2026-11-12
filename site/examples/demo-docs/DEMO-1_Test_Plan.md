---
plan_format: 1
project: DEMO-1
type: test-plan
status: approved
---

# DEMO-1 rev A: bring-up test plan

Synthetic example board for the Polybench documentation: a 12 V input, a 5 V buck, a 3.3 V LDO, a microcontroller (U1), an ADC (U2) and a CAN transceiver (U3). Every limit names its source. Polybench generates the Unpowered and Powered tests from the tier sections of this document; the visual, function and characterisation tests stay in the protocol file.

> [!note] How to read the limit classes
> **hard** limits come from a datasheet or a design calculation and can fail a board. **guess** limits are estimates: outside them a value is flagged for review, never failed. **tbr** limits are to be reviewed and give no verdict. **record** only records the value.

## Equipment

- Bench supply with current limit, or the power analyzer action `read_rails`
- Handheld meter on resistance and continuity

## Tier 1 — Unpowered

### Input and rails (J1, D1, C7, U1, U2, U3)

Meter on resistance. Red lead on Probe +, black on Probe −. **Board unpowered.**

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U01 | VIN to GND resistance | TP1 | TP2 | VIN, GND | J1, D1 | number | kOhm | 10 | — | hard | DEMOCAN DS-001 p.5 Table 2 | below 1: VIN looks shorted, do not power this board | — | — |
| U02 | 5V to GND resistance | TP3 | TP2 | 5V, GND | U2 | number | kOhm | 2 | — | guess | closes on: first powered board current | — | x0.5..x2 | — |
| U03 | 3V3 to GND resistance | TP4 | TP2 | 3V3, GND | U1 | number | kOhm | 1 | — | tbr | closes on: load figure in DEMOMCU datasheet, current revision | — | ±25% | — |

- U01: Wait two seconds for the input capacitors to charge before reading.

### Connector (J1)

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U04 | J1 pin 1 to VIN continuity | J1 | TP1 | VIN | J1.1 | yn | — | — | — | hard | DEMO-1 SCH p.1 Section Power input | on No: J1 pin 1 open, check the connector joint | — | — |
| U05 | Free note on the board | — | — | — | — | text | — | — | — | record | — | — | — | keyboard |

## Tier 1 — Powered

### First power (U1, U2, U3, C1, C2, C3, C4, C5, C6)

Actions: read_rails
Set the supply to 12 V, current limit 150 mA. Switch on, read, switch off.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P01 | Supply current at 12 V | — | — | VIN | — | number | mA | — | 40 | hard | calc C1 | above 100: over-current, switch off now | ±25% (min 2 mA) | keyboard, voice, action |
| P02 | 5V rail | TP3 | TP2 | 5V | U2 | number | V | 4.85 | 5.15 | hard | DEMOBUCK DS p.3 Table 1 | outside 4.5..5.5: rail out of range, switch off | — | keyboard, voice, action |
| P03 | 3V3 rail | TP4 | TP2 | 3V3 | U1 | number | V | 3.2 | 3.4 | hard | DEMOLDO DS p.4 Table 1 | — | — | keyboard, voice, action |
| P04 | Idle current after boot | — | — | VIN | — | number | mA | — | 25 | guess | closes on: measurement on the first 3 boards | — | ±25% | keyboard, voice, action |

## Tier 2 — Unpowered (deeper review)

### Rail fault finding (C1, C2, C3, C4, C5, C6)

Only for boards the decision gate sends here. Lift one end of each decoupling capacitor in turn until the low reading clears.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U10 | 5V to GND with the capacitors lifted | TP3 | TP2 | 5V, GND | C1, C2, C3 | number | kOhm | — | — | record | — | — | — | — |
| U11 | 3V3 to GND with the capacitors lifted | TP4 | TP2 | 3V3, GND | C4, C5, C6 | number | kOhm | — | — | record | — | — | — | — |
| U12 | Faulty part found | — | — | — | C1, C2, C3, C4, C5, C6 | yn | — | — | — | record | — | — | — | — |

- U12: Answer Yes when the faulty part is found, and name it in a note.

## Decision gate

A board goes to Tier 2 when any Tier 1 unpowered reading is out of band or trips a stop rule, or when it is an outlier against the batch once five boards are measured. It is not powered until Tier 2 is done and Tier 1 has been repeated.

```gate
after: Tier 1 Unpowered
to: Tier 2 Unpowered
any out of band
any stop
outlier from 5 boards
hold power
halt when more than 50 % of boards are out of band on one test
```

## Design calculations

- C1: 12 V input current budget: MCU 12 mA, ADC 3 mA, CAN 5 mA at 5 V through the buck at 85 % efficiency gives 9.8 mA at 12 V; 40 mA leaves margin for the LDO and start-up.

## Open items

- U03: replace the estimate with the load figure from the DEMOMCU datasheet.
- P04: set from the first three boards.
