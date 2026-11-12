---
type: demo-fixture
---

# DEMO-1 smoke test

Synthetic smoke-test sheet for the voice tests. Type or speak readings into the table at the end.

## 1. Before power

| ID | Check | Pass | Stop if |
|---|---|---|---|
| C04 | `VIN` to GND, ohms | OL | below 10 kΩ |

## Results

| ID | Check | Unit | Expected | B1 | B2 | B3 | B4 | B5 | Pass/fail |
|---|---|---|---|---|---|---|---|---|---|
| AOI_clear | AOI candidates for this board resolved | Y/N | Y | | | | | | |
| V04 | U2 regulator: no bridge on pins 3 to 6; R8 fitted, R2 empty | OK/text | no bridge; R8 fitted; R2 empty | | | | | | |
| V07a | D3, D2 band direction | OK/text | per checklist V07 | | | | | | |
| C04 | `VIN` pad to GND, red on `VIN`, ohms | Ω | OL or rising to OL; defect below 10 kΩ | | | | | | |
| C05 | `VIN` pad, red on GND, diode range | V | OL (0.6 to 0.9 V means D3 reversed) | | | | | | |
| C06 | `VIN` pad, red on `VIN`, diode range | V | OL (0.8 to 1.0 V means D2 reversed) | | | | | | |
| C07 | `3V3` pad to GND, red on `3V3`, ohms | Ω | high; below 1 kΩ is a defect | | | | | | |
| C08 | `3V3` pad to GND, red on GND, diode range | V | diode drop 0.3 to 1.3 V; 0.00 V is a short | | | | | | |
| PV05 | `3V3` pad at supply 5.0 V | V | 3.27 to 3.33 | | | | | | |
| PI05 | Supply current at 5.0 V | mA | TBR; below 100 | | | | | | |
| PV12 | `3V3` pad at supply 12.0 V | V | 3.27 to 3.33 | | | | | | |
| PI12 | Supply current at 12.0 V | mA | TBR; below 61 | | | | | | |
| P06_hot | After 30 s at 12 V, supply off: U2, L1, D3 not too hot, no smell, no hiccup seen | Y/N | Y | | | | | | |

