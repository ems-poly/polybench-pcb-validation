---
type: demo-fixture
---

# DEMO-1 bring-up results

Synthetic input sheet for the voice tests. Every ID is unique across the sheet; fill the board columns with the number read
(with its unit), or `OK` for a visual tick. In **Pass/fail** write `P`, or `F` plus the board numbers that failed.
Board 5 has no sensor: its sensor rows read `ABSENT` or `SKIP`. Nothing here was measured.

## Session record

| Item | Entry |
|---|---|
| Date and time | |
| Operator | |

## As-built record

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| V01 | U1 top marking | text | DEMO-MCU-A (design) | | | | | | |
| V08 | J2 fitted | Y/N | N (not fitted) | | | | | | |
| V09 | J1 connector fitted | Y/N | Y | | | | | | |
| V10 | Pigtail wire gauge | AWG or mm² | TBR | | | | | | |
| P19a | Flash and RAM report | text | 4 MB flash, 2 MB RAM | | | | | | |
| P19b | Chip revision and address | text | record | | | | | | |

## Stage V: visual

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| V02 | U1 pins and exposed pad wetted, no bridges | OK/text | all pins wetted, no bridges | | | | | | |
| V03 | U3 placement | OK/text | square, no tilt | | | | | | |
| V04 | U2 regulator: no bridge on pins 3 to 6 | OK/text | no bridge | | | | | | |
| V05 | U4 transceiver: no bridge on pins 6 and 7 | OK/text | no bridge | | | | | | |
| V06 | 0402 passives: none missing or tombstoned | OK/text | all present | | | | | | |
| V07 | D1 to D3 orientation | OK/text | as marked | | | | | | |
| V07a | D3, D2 band direction | OK/text | per checklist V07 | | | | | | |
| V11 | Bottom pads clean | OK/text | OK | | | | | | |
| V12 | Flux cleaned | OK/text | OK | | | | | | |
| V13 | Photos taken and captioned | Y/N | Y | | | | | | |

## Stage C: cold electrical

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| C01 | `BUS_H` to `BUS_L`, both polarities | Ω | OL (no termination) | | | | | | |
| C02 | `BUS_H` to GND, both polarities | Ω | OL, at least 1 MΩ | | | | | | |
| C03 | `BUS_L` to GND, both polarities | Ω | OL, at least 1 MΩ | | | | | | |
| C04 | `VIN` pad to GND, red on `VIN`, ohms | Ω | OL or rising to OL; defect below 10 kΩ | | | | | | |
| C05 | `VIN` pad, red on GND, diode range | V | OL (0.6 to 0.9 V means D3 reversed) | | | | | | |
| C06 | `VIN` pad, red on `VIN`, diode range | V | OL (0.8 to 1.0 V means D2 reversed) | | | | | | |
| C07 | `3V3` pad to GND, red on `3V3`, ohms | Ω | high; below 1 kΩ is a defect | | | | | | |
| C08 | `3V3` pad to GND, red on GND, diode range | V | diode drop 0.3 to 1.3 V; 0.00 V is a short | | | | | | |
| C09 | R3, `3V3` pad to `3V3_AUX` | Ω | 21.8 to 22.2 | | | | | | |
| C10 | R4, `EN` pad to `3V3` pad | kΩ | 9.9 to 10.1 | | | | | | |
| C11 | R8 link, `VCC_A` to `MODE_B` | Ω | below 1 | | | | | | |
| C12 | R2 pad `MODE_B` to GND | Ω | OL | | | | | | |
| C13 | R6, `3V3` to `LED_A` | Ω | 217.8 to 222.2 | | | | | | |

### C14: ground network

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| C14a | G ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14b | C2 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14c | C1 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14d | C5 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14e | C22 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14f | C6 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14g | C8 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14h | C9 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14i | C10 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14j | C7 ground pad | mΩ or Ω | connected; TBR | | | | | | |
| C14k | J1 shell ground pad | mΩ or Ω | connected; TBR | | | | | | |

## Stage P: hot checks

### Ramp: 3V3 against supply voltage

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| PV02 | `3V3` pad at supply 2.0 V | V | 0 | | | | | | |
| PV03 | `3V3` pad at supply 3.0 V | V | 0 | | | | | | |
| PV04 | `3V3` pad at supply 4.0 V | V | 3.0 to 3.6 | | | | | | |
| PV05 | `3V3` pad at supply 5.0 V | V | 3.27 to 3.33 | | | | | | |
| PV09 | `3V3` pad at supply 9.0 V | V | 3.27 to 3.33 | | | | | | |
| PV12 | `3V3` pad at supply 12.0 V | V | 3.27 to 3.33 | | | | | | |
| PV16 | `3V3` pad at supply 16.0 V | V | 3.27 to 3.33 | | | | | | |

### Ramp: supply current

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| PI02 | Supply current at 2.0 V | mA | microamps; stop at 1 mA | | | | | | |
| PI03 | Supply current at 3.0 V | mA | microamps; stop at 1 mA | | | | | | |
| PI04 | Supply current at 4.0 V | mA | TBR; below 100 mA | | | | | | |
| PI05 | Supply current at 5.0 V | mA | TBR; below 100 | | | | | | |
| PI09 | Supply current at 9.0 V | mA | TBR | | | | | | |
| PI12 | Supply current at 12.0 V | mA | TBR; below 61 | | | | | | |
| PI16 | Supply current at 16.0 V | mA | TBR | | | | | | |

### Steady state at 12 V

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| P08 | Idle supply current, blank flash, 30 s settled | mA | TBR; below 61 | | | | | | |
| P09a | `VCC_A` at R8 pad | V | 3.1 to 3.45 | | | | | | |
| P09b | MODE level at R2 pad | V | above 1.6, equal to VCC | | | | | | |
| P10a | SW frequency at L1 pin 1 | MHz | 2.1 to 2.3, continuous | | | | | | |
| P11 | `3V3_AUX` minus `3V3` | mV | within 5 | | | | | | |
| P14 | `3V3` ripple, AC, 20 MHz BW | mV pk-pk | record only | | | | | | |
| P16a | `BUS_H` recessive, no firmware | V | 1.5 to 2.25 (typ 1.9) | | | | | | |
| P16b | `BUS_L` recessive, no firmware | V | 1.5 to 2.25 (typ 1.9) | | | | | | |
| P06_hot | After 30 s at 12 V, supply off: U2, L1, D3 not too hot, no smell, no hiccup seen | Y/N | Y | | | | | | |

### USB, boot and first firmware

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| P17 | USB enumeration (VID:PID) | text | 1234:5678 | | | | | | |
| P18 | Download mode entered, flash tool connects | Y/N | Y | | | | | | |
| P19 | Flash tool chip and flash info (values in P19a and P19b) | text | see P19a and P19b | | | | | | |
| P20 | LED red / green / blue lit | Y/N x3 | all three | | | | | | |
| P21 | WHO_AM_I at 1 MHz | hex | B1 to B4: 0x50; B5: not 0x50 | | | | | | |
| P21b | WHO_AM_I at 8 MHz | hex | B1 to B4: 0x50; B5: not 0x50 | | | | | | |
| P24 | USB-only power: `3V3` | V | 3.27 to 3.33 | | | | | | |

## Firmware selftest

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| MAC | Address from the boot banner | text | record | | | | | | |
| HEAP | Free internal heap | B | PASS: at least 150000 | | | | | | |
| RAIL_3V3 | `3V3` measured at the pad with the DMM | V | 3.27 to 3.33; firmware verdict N/A | | | | | | |
| LED | Red, green, blue, white 1 s each; operator answers y/n | y/n | PASS: operator answers y | | | | | | |
| SENS_ID | WHO_AM_I | hex | PASS: 0x50; B5: ABSENT | | | | | | |
| SENS_G | Mean per axis, gravity axis (write g and axis) | g | PASS: dominant axis 0.3 to 2.5; B5: SKIP | | | | | | |
| SENS_RATE | Rows per second between FIFO reads | Hz | PASS: 10000 within 1.5 %; B5: SKIP | | | | | | |
| CAN_LB_INT | Controller alone, internal loopback, 8 frames | count | PASS: 8 of 8 received | | | | | | |
| CAN_LB_XCVR | The same through the transceiver | count | PASS: 8 of 8 received | | | | | | |

## Power and efficiency

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| mode_link | R0: R8 fitted, R2 not fitted (visual) | Y/N | Y | | | | | | |
| iin_5v | R1: Ch1 current at 5 V, MCU in reset, 0.10 A limit | mA | TBR | | | | | | |
| iin_9v | R1: Ch1 current at 9 V, same state | mA | TBR | | | | | | |
| iin_12v | R1: Ch1 current at 12 V, same state | mA | TBR | | | | | | |
| iin_16v | R1: Ch1 current at 16 V, same state | mA | TBR | | | | | | |
| vcc_v | R3: `VCC_A` at C6, 12 V, 1 mA | V | 3.1 to 3.45 | | | | | | |
| fsw_khz | R4: SW centre frequency at L1 pin 1, 1 mA | kHz | 2100 to 2300 | | | | | | |
| fsw_span_khz | R4: SW frequency span | kHz | record | | | | | | |
| d3_drop_1ma | D3 drop, sink 1 mA, 12 V | mV | record (no limit) | | | | | | |
| d3_drop_100ma | D3 drop at sink 100 mA | mV | record | | | | | | |
| d3_drop_180ma | D3 drop at sink 180 mA | mV | record | | | | | | |
| ripple_pp_mv_off | R7: ripple pk-pk at C22, 20 MHz limit, load off, 12 V | mV | TBR (record only) | | | | | | |
| ripple_pp_mv_1ma | R7: ripple pk-pk at C22, load 1 mA | mV | TBR (record only) | | | | | | |
| ripple_pp_mv_180ma | R7: ripple pk-pk at C22, load 180 mA | mV | TBR (record only) | | | | | | |
| ripple_c21_mv | R8: ripple at C21, 180 mA | mV | TBR (record only) | | | | | | |
| eta_12v_10ma | Efficiency, 12 V, sink 10 mA | % | TBR | | | | | | |
| eta_12v_180ma | Efficiency, 12 V, sink 180 mA | % | TBR | | | | | | |

## Findings and defects

| # | Board | Check ID | What was found | Action | Owner |
|---|---|---|---|---|---|
| | | | | | |
