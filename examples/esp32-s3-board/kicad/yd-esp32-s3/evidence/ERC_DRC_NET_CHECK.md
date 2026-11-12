# ERC and DRC report (KiCad 10.0.7, kicad-cli)

HOST-ONLY. Generated 2026-11-06 from the committed files; the same commands reproduce it.

## ERC (`kicad-cli sch erc --severity-all`)

**0 violations (0 errors, 0 warnings).** Net labels are global labels so that net names carry no sheet prefix; power nets use power symbols and three PWR_FLAGs (GND, 5V, VBUSB) because no power-output pin drives them. Ten pins are marked no-connect on purpose: U1.2 (DO), U2 pins 10, 11, 14, 15, 16 (ACT#, DCD, DSR, CTS, RI), USB1 and USB2 pins A8, B8 (SBU).

## DRC (`kicad-cli pcb drc --schematic-parity --severity-all`)

- Violations: **132** (63 errors, 69 warnings), none about routing (there is none).
- Unconnected items (ratsnest, no copper routing by design): **145**.
- Schematic parity: **0 issues** (every footprint matches its symbol, value, footprint field and nets; NC pins carry the schematic's `unconnected-(...)` nets).

| Type | Count | Severity | Why it is there |
|---|---|---|---|
| copper_edge_clearance | 44 | error | Header pad rings and the USB-C shield pads sit 0.42 mm or less from the 57.15 x 27.94 mm outline of the maker's drawing (default rule 0.5 mm). The outline is the maker's; the board is not for manufacturing. |
| text_height | 33 | warning | Designators of 0402 parts are 0.6 mm high (default silk minimum 0.8 mm) so that eleven parts fit in 1 mm columns. Cosmetic. |
| silk_overlap | 18 | warning | Reference designators of 0402 parts only 1 mm apart touch neighbouring outlines. Kept visible rather than hidden (the brief wants visible designators). |
| drill_out_of_range | 12 | error | Library footprint ESP32-S3-WROOM-1 has 0.2 mm thermal vias in its ground pad (default minimum 0.3 mm). Taken from the KiCad library as is. |
| silk_edge_clearance | 12 | warning | USB-C, header and J1/J2 silkscreen and designators run to or over the outline (connector shells overhang the edge on the real board). |
| courtyards_overlap | 7 | error | Real neighbours: USB connector shells and the header row, the jumper pads beside D3 / U1, and R5 beside Q1 sit within each other's library courtyards on the maker's layout. Not an error of the redraw. |
| silk_over_copper | 6 | warning | Designator text touches a neighbouring pad (clipped by the solder mask). Cosmetic. |

The default board rules were left as they are; nothing was relaxed to hide a finding. Errors are all library or outline facts, not connectivity.

## Net check against the PDF

Expected nets were typed net by net from SCH V1.4 and compared with KiCad's own netlist export of the redrawn schematic (`kicad-cli sch export netlist`): **43 parts, 59 nets, 0 mismatches, 0 extra nets**; the 10 unconnected pins are exactly the no-connect list above.

| Net | Pins | Source | Result |
|---|---|---|---|
| 5V | 8 | PDF label | match |
| CHIP_PU | 7 | PDF label | match |
| D+ | 3 | PDF label | match |
| D- | 3 | PDF label | match |
| DTR | 3 | PDF label | match |
| GND | 38 | PDF label | match |
| GPIO0 | 5 | PDF label | match |
| GPIO1 | 2 | PDF label | match |
| GPIO10 | 2 | PDF label | match |
| GPIO11 | 2 | PDF label | match |
| GPIO12 | 2 | PDF label | match |
| GPIO13 | 2 | PDF label | match |
| GPIO14 | 2 | PDF label | match |
| GPIO15 | 2 | PDF label | match |
| GPIO16 | 2 | PDF label | match |
| GPIO17 | 2 | PDF label | match |
| GPIO18 | 2 | PDF label | match |
| GPIO19 | 4 | PDF label | match |
| GPIO2 | 2 | PDF label | match |
| GPIO20 | 4 | PDF label | match |
| GPIO21 | 2 | PDF label | match |
| GPIO3 | 3 | PDF label | match |
| GPIO35 | 2 | PDF label | match |
| GPIO36 | 2 | PDF label | match |
| GPIO37 | 2 | PDF label | match |
| GPIO38 | 2 | PDF label | match |
| GPIO39 | 2 | PDF label | match |
| GPIO4 | 2 | PDF label | match |
| GPIO40 | 2 | PDF label | match |
| GPIO41 | 2 | PDF label | match |
| GPIO42 | 2 | PDF label | match |
| GPIO45 | 2 | PDF label | match |
| GPIO46 | 2 | PDF label | match |
| GPIO47 | 2 | PDF label | match |
| GPIO48 | 3 | PDF label | match |
| GPIO5 | 2 | PDF label | match |
| GPIO6 | 2 | PDF label | match |
| GPIO7 | 2 | PDF label | match |
| GPIO8 | 2 | PDF label | match |
| GPIO9 | 2 | PDF label | match |
| RGB_CTRL | 2 | PDF label | match |
| RTS | 3 | PDF label | match |
| U0RXD | 4 | PDF label | match |
| U0TXD | 4 | PDF label | match |
| VBUSB | 8 | PDF label | match |
| VDD33 | 14 | PDF label | match |
| 5V_HDR | 3 | unnamed in PDF, name ours | match |
| CH343_V3 | 2 | unnamed in PDF, name ours | match |
| GPIO3_PU | 2 | unnamed in PDF, name ours | match |
| LED_PWR_A | 2 | unnamed in PDF, name ours | match |
| LED_RX_A | 2 | unnamed in PDF, name ours | match |
| LED_TX_A | 2 | unnamed in PDF, name ours | match |
| Q1_B | 2 | unnamed in PDF, name ours | match |
| Q2_B | 2 | unnamed in PDF, name ours | match |
| USB1_CC1 | 2 | unnamed in PDF, name ours | match |
| USB1_CC2 | 2 | unnamed in PDF, name ours | match |
| USB2_CC1 | 2 | unnamed in PDF, name ours | match |
| USB2_CC2 | 2 | unnamed in PDF, name ours | match |
| VBUS2 | 6 | unnamed in PDF, name ours | match |
