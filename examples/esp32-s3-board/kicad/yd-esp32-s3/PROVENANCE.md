# Provenance: YD-ESP32-S3 KiCad redraw

**Redrawn by Polystage for the Polybench example; not VCC-GND's design files; not for manufacturing.**

This folder is a KiCad 10 project of the VCC-GND Studio YD-ESP32-S3 (N16R8) dev board, drawn so that Polybench's KiCad plug-in has a board with real designators, nets and footprint positions to highlight. It has a schematic and a board with parts placed where they are on a real unit and **no copper routing** (ratsnest only). It is not a manufacturing file and must not be used as one: footprints are KiCad library parts chosen to fit, pad shapes and the outline are approximations, and the layers hold no copper.

Labels used below: HOST-ONLY (checked on the host with kicad-cli and scripts, no KiCad GUI, no board), UNVALIDATED (not checked at all against hardware), STATED BY OPERATOR (a fact the bench operator told us; not measured, not photographed).

## Sources

| Source | Where | Revision | SHA-256 | Used for |
|---|---|---|---|---|
| VCC-GND Studio, YD-ESP32-S3 schematic "YD-ESP32-S3-COREBOARD V1.4", one sheet, dated 2022-09-23 | https://github.com/vcc-gnd/YD-ESP32-S3 , `5-public-YD-ESP32-S3-Hardware info/YD-ESP32-S3-SCH-V1.4.pdf` | repo commit `b4ac4d60d1845a554bbbc7c97806ccb97cf08941` (2023-10-29) | `ec1844f0e7d736b7ccca556e6cec6f1876f5ee7d661b249d0f780e4124790609` | Every part, value, designator and connection (read by eye from a 300 dpi render; the text layer is incomplete) |
| VCC-GND Studio, layout drawing `ESP32-S3-Metric.pdf` (a vector print of the top layer, with pad names and designators) | same repo and folder | same commit | `23098cc8e1195bff1f6040cca85c4e2536a6a7eae21d3bda1c2c2962ec3cbd19` | Outline (27.94 x 57.15 mm; 63.39 mm with the antenna), header grid, and **seed** positions and rotations of top-side parts (see Method) |
| VCC-GND Studio, Altium board file `ESP32-S3-size-pcb-ad.PcbDoc` | same repo and folder | same commit | `6321af6036af76eb165f7acfc644aaf71ab4db7ee75831715062d98ad3066c35` | Not opened (binary Altium). Its content reaches this redraw only through the two rows above and the next row |
| shkuznetsov/YD-ESP32-S3, `YD-ESP32-S3.kicad_mod` (converted from the Altium file above), MIT, Copyright (c) 2025 Alexander Kuznetsov | https://github.com/shkuznetsov/YD-ESP32-S3 | commit `bb410449c3d5ee20096ff273d6c1081c5dd6228f` (2025-11-22) | `d440ba7f9e1c87d4e42fdb8edc33462612284230e83fa24ed5e8b6a6367e1721` | Cross-check only: its 28 x 57 mm rectangle, header row spacing 25.4 mm, pitch 2.54 mm and first pad 1.91 mm from the end agree with the drawing to 0.06 mm. No file or line is copied; the headers here are KiCad's standard `PinHeader_1x22_P2.54mm_Vertical` |
| Photo of the maker's board, unit D01 (the clean unit), `examples/esp32-s3-board/aoi/D01_top.jpg`, 2000 px, handheld, near top-down, EXIF removed | this repository | commit `2f5fa39` | `d75456f9b039be7a3e3ee8b3e622b48fc29db9569e9d0d3f3e5eca86c566a211` | Rectification, verification and correction of positions; part sizes and markings |
| KiCad 10.0.7 standard symbol and footprint libraries (CC-BY-SA 4.0 with the KiCad library exception) | KiCad install | 10.0.7 | | Symbols and footprints, copied into the files as KiCad does |

VCC-GND's repository carries no licence file for the PDF and the drawing. They are used as sources of facts (which part, which value, which pin joins which net, where a part sits); nothing graphical is copied from them. The redraw is released under the licence below on the basis of the maintainer's decision of 6 November 2026.

## Licence

MIT, with attribution to the sources above: the connections, values, designators and layout facts are VCC-GND Studio's (schematic V1.4 and layout drawing); the outline and header-grid cross-check is by Alexander Kuznetsov (MIT, shkuznetsov/YD-ESP32-S3); the photo is the maintainer's. The KiCad library parts keep their own licence. See also `THIRD_PARTY_NOTICES.md` at the repository root.

## Method

1. **Schematic.** The PDF was rendered at 300 dpi and read quadrant by quadrant. Every part and every wire was entered net by net into `tools/design.py`: 43 parts (U1 to U4, Q1, Q2, D1 to D3, R1 to R11, C1 to C10, USB1, USB2, J1, J2, TX, RX, PWR, BOOT, RST and the four jumpers RGB, IN-OUT, USB-OTG, USB-JTAG; a text scan of the PDF finds 38 because it misses TX, RX, PWR, BOOT and RST) and 46 nets that the PDF names (GND, 5V, VDD33, VBUSB, D+, D-, U0TXD, U0RXD, RTS, DTR, CHIP_PU, RGB_CTRL and 34 GPIO nets; the scan's "about 32" is low). Thirteen more nets have no name in the PDF and carry a name of ours (list below). `tools/gen_sch.py` writes the schematic with KiCad's own symbols; three symbols are project-local (`yd_local.kicad_sym`: CH343P, XL-5050RGBC-WS2812B, CJ6107A33GW) because no library symbol fits. Nets are carried by global labels and power symbols, not by long wires.
2. **Net check.** Expected nets were typed again, net by net, from the PDF (`tools/netcheck.py`) and compared with KiCad's own netlist export: 59 nets, 0 mismatches (`evidence/ERC_DRC_NET_CHECK.md`). ERC: 0 violations.
3. **Rectification.** The 44 header holes were found in `D01_top.jpg` (blue-green mat showing through the holes, connected components) and the photo was warped to board millimetres by a homography fitted on four pads (J1.1, J1.22, J2.1, J2.22), 24 px/mm (`tools/rectify.py`, `evidence/rectified_D01_top.png`).
4. **Placement.** The layout drawing's pad rectangles were matched to each footprint's pads (Procrustes, four rotations) to give position and rotation of 33 top-side parts, with a residual of 0.02 to 0.34 mm against the library pad pattern (`tools/pdfparts.py`, `tools/placement.py`). This seed was then compared part by part with the rectified photo at 3x zoom (`evidence/overlay_pcb_on_rectified_photo.png`). Corrected from the photo: U2 (the drawing shows a 10-pad part, the photo a QFN-16 with the pin 1 dot top-right), C1 and C2 (the drawing puts them right of the old U2, the photo shows two 0402 capacitors above the new one), and C10 (a lone 0402 capacitor beside U2 that the drawing does not have). Diodes D1 to D3 are turned so that the cathode band seen in the photo (the side away from the USB connectors) is the cathode.
5. **Board.** `tools/build_pcb.py` (KiCad's Python) places the library footprints, assigns pad nets from the same table as the schematic, draws the outline and sets designators visible on the silkscreen. `kicad-cli pcb drc --schematic-parity`: 0 parity issues.

The scripts in `tools/` are the ones that produced the files; their dev-machine paths were replaced by `YD_WORK` and `YD_REPO` before the commit and the committed copies have not been re-run (UNVALIDATED reproducibility; the kicad-cli checks above were run on the produced files).

## Achieved accuracy (HOST-ONLY)

- **Rectification, measured on the header pads not used in the fit:** four pads fitted, the other 40 pads reprojected into board millimetres: **mean 0.037 mm, rms 0.044 mm, maximum 0.092 mm**. This is the error of the pad plane (z = 0) only. It does not include the error of finding a part by eye, nor parallax of tall parts: the module can, USB-C shells, C3/C4 and the tact switches stand 1 to 3 mm above the pad plane and appear shifted by up to about 0.2 mm in the photo.
- **Part positions:** target was +/-0.5 mm (TBR). Not measured numerically per part. Against the rectified photo at 3x zoom every top-side part sits within about 0.3 mm of its pads (largest visible offsets: Q1, Q2 pads, U2 pads); the placement is the layout drawing's, whose own pad rectangles fit the library footprints to 0.02 to 0.34 mm. A numerical per-part measurement needs a straight-on photo with the board flat (see the bottom photo below, which also gives a second top photo).
- The headers are exact by construction (2.54 mm grid, 25.4 mm apart, 1.91 mm from the antenna-end edge).

## Bench facts (STATED BY OPERATOR, 10 Nov 2026; not measured)

The maintainer who owns the bench boards stated the following on 10 November 2026. They are facts about the operator's own boards, taken as given; nothing here was measured or photographed by us. (Per the public-tree rule the maintainer is not named in this file; the named citation is in the internal `VERIFICATION.md` of `examples/esp32-s3-board/`, which the public tree excludes.)

- **F1, jumpers.** On all of the operator's boards no solder jumper is bridged: RGB is open and IN-OUT is open.
- **F2, USB ports.** Looking at the USB end with the component side up, the left port is the native USB (USB2, OTG) and the right port is COM, the CH343P UART (USB1). The words stated were "left is USB, right is COM". **The viewing orientation (at the USB end, component side up) is our interpretation, to be confirmed by the operator.** It is consistent with the photo-derived position (USB1 on the J2 header side): with the antenna to the right and J1 the top row in `D01_top.jpg`, a viewer at the USB end looking towards the antenna with the component side up has the J1 side on the left and the J2 side on the right.

Consequences carried into the plan (`YD_ESP32_S3_TEST_PLAN.md`, r2) and `function.yaml`: the RGB LED does not reach GPIO48 on these boards, so no test expects it to light (F09 retired, F12 records the observation); the 5V header pin is not driven by USB power (P05 retired, P06 records the reading); the jumper tests U06 and U07 stay as records, so a board that differs is noticed.

## J1.21 with IN-OUT open, USB-powered, no load (derivation, HOST-ONLY)

From the redraw's netlist, which was entered from SCH V1.4 (`tools/design.py`; the connectivity is the one `tests/test_yd_kicad_redraw.py` checks):

1. Net `5V_HDR` has three pads: J1.21, D3 pin 2 and IN-OUT pin 1. IN-OUT pin 2 is on the 5V rail. Nothing else touches `5V_HDR`.
2. D3: pin 2 (anode) is on `5V_HDR`, pin 1 (cathode) is on `5V` (KiCad `Device:D`: pin 1 = K, pin 2 = A). The photo's cathode band, on the side away from the USB connectors, is read the same way (T9: the marking S4 / value "D26" is not identified, so no forward-drop or leakage figure exists in this repository).
3. Powered from USB1 or USB2: VBUS goes through D1 (or D2), anode on VBUS and cathode on `5V`, so the 5V rail is VBUS minus D1's (or D2's) drop. The rail is at D3's **cathode**: D3 is reverse biased by the whole rail voltage. With IN-OUT open there is no other path from the rail to J1.21.
4. Result: J1.21 is **not driven** by USB power, and it is not "VBUS minus D3's drop" (that would need D3's anode on the rail; here it is the other way round and the header is an input). The node has no source and no load other than D3's reverse leakage and whatever is attached to it. A meter on it therefore reads a value set by that leakage into the meter's input resistance: between 0 V and the rail voltage (leakage cannot lift the node above the rail), and which value depends on the diode and the meter, neither of which is known here. The schematic does not settle the number.
5. Choice made: P06 is a `record` number, not a `guess` with limits; the expectation "floating, not driven" is stated in the plan text and is not a pass or fail condition. Had IN-OUT been bridged, J1.21 would sit at the 5V rail (VBUS minus D1's drop; the drop is not known here, T9), which is what the retired P05 limits (4.6 to 5.25 V) assumed.

Labels: the netlist-level result is HOST-ONLY; D3's orientation on the physical board rests on the photo band and the schematic (UNVALIDATED on a board); the open-jumper state is STATED BY OPERATOR.

## TBR list

| # | Item | Choice made | Why | How to close |
|---|---|---|---|---|
| T1 | **Bottom-side parts**: R11, USB-JTAG, USB-OTG | Placed on the bottom side at guessed positions (R11 and USB-JTAG near x 33 mm, y 21.5 and 24 mm; USB-OTG behind D2) | They are in the PDF but not on the top photo or the top-layer drawing | Bottom photo (below) |
| T2 | C10 (1 uF, VIO of U2) | Top side, on the lone 0402 capacitor beside U2 | Photo shows one capacitor the drawing lacks; U2's VIO needs one | Bottom photo and a continuity check |
| T3 | C1 / C2 assignment | C1 at the left, C2 at the right of the two capacitors above U2 | Both look the same; values are 1 uF and 100 nF | Continuity: C2 is on U2 pin 6, C1 on U2 pin 3 |
| T4 | Jumpers RGB and IN-OUT (and USB-OTG, USB-JTAG) | **Closed by an operator statement (see "Bench facts" below): no solder jumper is bridged on the operator's boards, so RGB and IN-OUT are open.** The model keeps them as two-pad open solder jumpers (`SolderJumper-2_P1.3mm_Open_TrianglePad`), which now matches the stated state | The PDF draws them as 0 ohm; the photo shows bare shaped pads; the operator states all are open (STATED BY OPERATOR, not measured). For USB-OTG and USB-JTAG the statement is the general wording "no solder jumper is bridged"; the two jumpers named in it are RGB and IN-OUT | Plan tests U06, U07 still record the state per board, so a board that differs is seen. A macro photo of the pads would turn the statement into evidence |
| T5 | U1 pin numbers | Symbol follows the part's own numbering (pin 3 GND, pin 4 DI); the PDF numbers them the other way round | The PDF's pad order matches the standard footprint only with 3 and 4 swapped; the connection is the same physically | None needed; for reference only |
| T6 | U3 part | Value CJ6107A33GW from the PDF, marking 1117C 33 D550 in the photo | Cannot tell if the marking is that part | Part datasheet or a pick-and-place report |
| T7 | Orientation of TX, RX, PWR LEDs and which boxed LED is which | As in the drawing (pad 1 taken as anode) | Not visible | Diode-test with a meter |
| T8 | C5, C6 packages | 0402 | PDF says C5 is 0603 and C6 0402; the photo shows two parts of the same small size | Photo at higher zoom |
| T9 | Diode package D1 to D3 | `D_SOD-123F` (pitch 2.8 mm fits the drawing's pads) | Marking S4; PDF value "D26" | Datasheet of the marked part |
| T10 | Pin 1 of U2 | Top-right in the photo (silk dot) | Orientation affects only which pad is called 1 | Datasheet drawing |
| T11 | Which USB-C is the UART port | **Closed by an operator statement (see "Bench facts" below):** USB1, the CH343P UART port (labelled COM), is the **right-hand** port looking at the USB end with the component side up; USB2, the native USB (OTG), is the left-hand port. This agrees with the earlier reading from the photo (USB1 beside U2 on the J2 side) | The schematic wires USB1 to U2; the statement names the port by side | The statement is "left is USB, right is COM"; the orientation above (viewed at the USB end, component side up) is our reading of it and is an interpretation for the operator to confirm. Plugging in and seeing which port enumerates as a serial device would still check it |
| T12 | Footprints for the module-side "tab" pin 4 of U3 | KiCad `SOT-223-3_TabPin2` (tab is pad 2) | The PDF draws OUT twice (pins 2 and 4); the footprint merges them | None |
| T13 | Resistor and capacitor packages overall | 0402 for all parts the PDF calls 0402, measured in the photo to be of that size | Pad pitch in the drawing is 0.87 mm, KiCad's 0402 is 1.02 mm; the difference is under 0.08 mm per pad | None |
| T14 | J1.21 reading, USB-powered, IN-OUT open, no load (added 10 Nov) | P06 records it; no limit | The schematic gives "not driven", not a number (D3's leakage into the meter) | Read J1.21 to J1.22 on a USB-powered board, name the meter; repeat with a second meter or range to see the meter dependence |

Names given by us to nets that the PDF leaves unnamed: `USB1_CC1`, `USB1_CC2`, `USB2_CC1`, `USB2_CC2` (CC lines to R2, R3, R9, R10), `CH343_V3` (U2 pin 6 to C2), `VBUS2` (USB2 VBUS before D2 and USB-OTG), `5V_HDR` (J1.21, the anode side of D3 and IN-OUT), `GPIO3_PU` (R11 to USB-JTAG), `LED_TX_A`, `LED_RX_A`, `LED_PWR_A` (resistor to LED anode), `Q1_B`, `Q2_B` (base resistors).

## Differences found between the sources

- The layout drawing's U2 (10 pads, two rows) is not the QFN-16 CH343P of the schematic and the photo: the drawing is of another revision, or of another bridge. Placements near U2 (C1, C2) follow the photo.
- The schematic's pin 4 of the regulator (tab) is pad 2 in the KiCad footprint.
- USB-C: the PDF draws one 16-pin symbol with merged pins (A1B12, A4B9, B1A12, B4A9); KiCad's symbol has the same connector with 17 separate pins. Electrically identical.

## What to photograph to close T1 to T3

Bottom of the board, **straight-on**: board flat on a matt surface (the teal mat is fine), camera directly above the middle of the board at 30 to 40 cm with the phone parallel to the table (use a spirit-level app), long edge horizontal, the antenna end on the **right** as in `D01_top.jpg` (turn the board over about its long axis, so the antenna stays on the right and the two header rows swap places), even light without glare on the pads, whole board and both header rows in frame, sharp. Take a second one with a coin or a 2.54 mm header pin row in frame for scale if easy. A close-up (macro) of each of the RGB and IN-OUT pad pairs from the top would turn the operator's statement that they are open (T4) into evidence; it is optional now.

## Live check in KiCad (5 steps for the maintainer; not run here, KiCad was not running)

1. In KiCad 10, Preferences, Plugins: enable the KiCad API server and restart KiCad; open `examples/esp32-s3-board/kicad/yd-esp32-s3/yd-esp32-s3.kicad_pcb` in the PCB editor (do not save anything).
2. In a clone: `.venv/bin/pip install -e '.[kicad]'`, then `.venv/bin/polybench doctor --ecad kicad --protocol examples/esp32-s3-board/yd-esp32-s3.pcbv.yaml`. Expect the "KiCad API" line OK with 43 parts.
3. Start `.venv/bin/polybench --protocol examples/esp32-s3-board/yd-esp32-s3.pcbv.yaml --ecad kicad --simulate --port 8901` (simulate keeps the instruments off; `--ecad kicad` selects the real bridge).
4. In Unpowered testing select U04, then U05, U06, U07, then the Powered tests P01, P02, P06, P04: each should select the named pads, parts and net pads in the PCB editor and zoom to them (compare with `evidence/*_highlight.png`).
5. Look at what KiCad selected: U04 = pads J1.21 and J1.22 plus the nets 5V_HDR and GND; U07 = RGB and U1 plus GPIO48 and RGB_CTRL. Any name KiCad does not find is reported by the plug-in; report it.

## Files

| File | What |
|---|---|
| `yd-esp32-s3.kicad_pro`, `.kicad_sch`, `.kicad_pcb` | The project |
| `yd_local.kicad_sym`, `sym-lib-table` | Three project-local symbols |
| `evidence/` | Board render, overlay on the rectified photo, one highlight image per test with a Highlight or Nets cell, ERC/DRC/net-check report, plug-in name resolution table |
| `tools/` | The scripts that produced the files |
