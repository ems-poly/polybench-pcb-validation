# Polybench demo: an ESP32-S3-WROOM-1 dev board (YD-ESP32-S3 or ESP32-S3-DevKitC-1)

A complete bench validation of a board you can buy for a few pounds. It shows what Polybench does with a real design: a visual step, unpowered and powered tests generated from a markdown test plan, a decision gate that holds a suspect board back, then flashing, identifying and self-testing the board over USB with plug-in actions.

It runs fully in `--simulate` mode with no board. With a board plugged in, the three function actions talk to the real chip (that path is UNVALIDATED, see below).

## Two variants, one choice: the protocol file

| Variant | Protocol file | Board | Visual step |
|---|---|---|---|
| **`yd-esp32-s3`** (default) | `yd-esp32-s3.pcbv.yaml` | VCC-GND Studio YD-ESP32-S3 N16R8: ESP32-S3-WROOM-1 N16R8, **two USB-C** ports, a USB-UART bridge marked CH343P, RGB LED on **GPIO48** | **Layout-mode AOI** on two photos (`aoi/`, layout from the redraw; the experimental diff mode is one line away); a KiCad redraw (`kicad/yd-esp32-s3/`) serves the ECAD highlight |
| `devkitc-1` | `devkitc-1.pcbv.yaml` | Espressif ESP32-S3-DevKitC-1 v1.1: Micro-USB, CP2102N-class bridge, RGB LED on GPIO38 | yes/no checklist |

The YD variant has its own test plan (`YD_ESP32_S3_TEST_PLAN.md`, designators, pads and nets of the KiCad redraw); DevKitC-1 keeps `ESP32_S3_BOARD_TEST_PLAN.md`. Both share `powered.yaml` and the firmware; the variant files are `assembly*.yaml`, `visual*.yaml`, `function*.yaml`.

Board facts for the YD variant come from the maker's schematic, VCC-GND Studio `YD-ESP32-S3-SCH-V1.4.pdf` (one sheet, "YD-ESP32-S3-COREBOARD V1.4", dated 2022-09-23, github.com/vcc-gnd/YD-ESP32-S3), which was read for this demo and redrawn in KiCad (`kicad/yd-esp32-s3/`, with `PROVENANCE.md`; the PDF itself is linked, not copied). What it says (sheet grid references in brackets), HOST-ONLY and not yet checked on a board:

- **UART bridge.** U2, CH343P (A3-A4). Its D+/D- come from USB1 (A1) and its TXD/RXD cross to the module's U0RXD/U0TXD (UART0). DTR and RTS reach CHIP_PU (EN) and GPIO0 through the two SS8050 transistors Q1, Q2 (B2-B3), the usual auto-program circuit; the sheet carries its truth table.
- **Native USB.** USB2 (C1) goes straight to the module: D+ on GPIO20, D- on GPIO19, with 5.1 k CC pull-downs R9, R10. USB1 carries R2, R3 the same way.
- **Power.** The 5V rail is fed through one diode per source: D1 from USB1 VBUS, D2 from USB2 VBUS (a solder jumper USB-OTG sits in parallel with D2), D3 from header pin J1.21 (a solder jumper IN-OUT sits in parallel with D3) (A1, C1, C6). U3, CJ6107A33GW (SOT-223, B5), makes VDD33 (3.3 V) from 5V. The power LED PWR sits on VDD33 through R6, 1 k (B6). TX and RX LEDs sit on U0TXD and U0RXD with 1 k resistors to VDD33 (A5).
- **RGB LED.** U1, XL-5050RGBC-WS2812B, is powered from 5V and its data input RGB_CTRL is fed from **GPIO48 through the solder jumper RGB** (A6). The sheet draws it as a 0 ohm resistor; in `aoi/D01_top.jpg` the RGB and IN-OUT jumpers look like bare shaped pads with nothing fitted. **Jumper state (stated by the operator, 10 Nov 2026, not measured): no solder jumper is bridged on the operator's boards, so RGB and IN-OUT are open.** With RGB open GPIO48 does not reach the LED: the LED cannot light, no test expects it to, and the serial self-test only records what you saw (F12, record). The plan tests U06 and U07 record the state per board, so a board that differs is noticed. With IN-OUT open the 5V header pin J1.21 is not driven by USB power (it is the input side of D3); plan test P06 records what a meter reads there.
- **Switches.** BOOT pulls GPIO0 to ground, RST pulls CHIP_PU to ground, each with a 100 nF capacitor (D2-D3).
- **Headers.** J1: pins 1 and 2 are 3V3, pin 3 is RST (CHIP_PU), pin 21 is the 5V pin (behind D3, see above), pin 22 is GND. J2: pins 1, 21 and 22 are GND, pin 2 is TX (U0TXD), pin 3 is RX (U0RXD) (C5-C7). There is no J3.
- **Strapping.** GPIO3 is pulled up through R11 and a jumper USB-JTAG (C2); the sheet gives no part for the module's other straps.

Differences between the sheet and the photo are listed in `kicad/yd-esp32-s3/PROVENANCE.md` (TBR list). A third-party description of the board exists at mischianti.org (CC BY-NC-ND): linked, not copied, not used.

**Which USB-C port is the UART port: stated by the operator (10 Nov 2026), consistent with the schematic and the photo, UNVALIDATED on a board.** The operator's words are "left is USB, right is COM". Read as: looking at the USB end with the component side up, the **left** port is the native USB (USB2, OTG) and the **right** port is COM, the CH343P UART (USB1). The viewing orientation is an interpretation, for the operator to confirm. The schematic wires USB1 to the CH343P (U2) and USB2 to the module's own USB pins. In the maker's layout drawing and in the photo, U2 sits beside one of the two connectors: the one on the **J2 header side** (the row with TX and RX; the lower connector when the antenna is to the right and J1 is the top row). That connector is USB1, the UART port. The self-test and the flash action need it. Confirm by plugging in: the bridge port appears as a serial device (`/dev/cu.wchusbserial*` with the CH343 driver), the native port as an Espressif USB device; the first run closes this.

**LED pin:** GPIO48 is confirmed by the schematic (above), but the RGB jumper is open on the operator's boards (stated), so it does not reach the LED there; the LED observation is a record only (F12). The firmware takes the pin at run time, `led 48` (the YD variant's `function.yaml` sends it); `LED_GPIO_DEFAULT` in `firmware/platformio.ini` is the build-time default (38) and was not rebuilt.

## What is here

| Path | What |
|---|---|
| `devkitc-1.pcbv.yaml` | Top protocol file (`pcbv.protocol/2`). |
| `YD_ESP32_S3_TEST_PLAN.md` (YD), `ESP32_S3_BOARD_TEST_PLAN.md` (DevKitC-1) | The master for the Unpowered and Powered steps: Tier 1, a small Tier 2, a decision gate. Every limit is `guess` or `record` and names what closes it. |
| `kicad/yd-esp32-s3/` | KiCad 10 redraw of the YD board (schematic, board with real positions, no routing) for the ECAD highlight, with `PROVENANCE.md` and `evidence/`. Not for manufacturing. |
| `assembly.yaml` (YD), `assembly_devkitc-1.yaml` | BOM of the main parts. YD: the full parts list of SCH V1.4 with designators, sides and TBR marks. DevKitC-1: from Espressif's v1.1 user guide and schematic (revisions cited in the file). |
| `visual.yaml` (YD), `visual_devkitc-1.yaml` | The Visual step: YD uses layout-mode AOI on `aoi/` (diff mode by swapping one line, see the comment in the file); DevKitC-1 keeps the checklist. |
| `aoi/` | The two YD photos and how they were taken (`aoi/README.md`). |
| `powered.yaml`, `function.yaml` (YD), `function_devkitc-1.yaml` | Powered gate; Program and function: flash, identify, serial self-test. |
| `firmware/` | PlatformIO sketch (pioarduino, `esp32-s3-devkitc-1`) that prints a JSON self-test, `PROVENANCE.md` (toolchain, licences, hash) and `prebuilt/` with the factory image and `MANIFEST.txt` (its SHA-256). |
| `VERIFICATION.md` | What was run, with labels. |
| `screenshots/` | The simulated walk. |

Actions (in `src/polybench/plugins/`): `esptool.identify`, `esptool.flash` (`esp32.py`), `serial.query` (`serialq.py`). They propose values; they never judge. Each runs in a child process, has a simulator path, a `selftest()` and a `safe_state()`.

## Install and run (from a clone, in the repo root)

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[esp32,aoi]'
.venv/bin/polybench --protocol examples/esp32-s3-board/yd-esp32-s3.pcbv.yaml --simulate --port 8901
```

The `aoi` extra is there only because the server imports OpenCV, NumPy and Pillow unconditionally at start-up (a core bug, owned elsewhere); the extra goes away from this line once that import is lazy. Without it the server does not start (`ModuleNotFoundError`).

Open http://127.0.0.1:8901. Paths in the protocol are relative to the directory you start in, so start from the repo root.

## The AOI walk, YD variant (simulated, two boards; layout mode)

HOST-ONLY: two photos, not a live camera; **not HARDWARE-VALIDATED**. Start with `--boards 2`. Walk: `tests/e2e/aoi_layout_walk.py` (Playwright WebKit).

1. Open **Visual**. Both tiles, D01 and D02, already carry their photo: the protocol names them (`aoi.photos`), no drag and drop (journal `assigned_by: protocol`).
2. Click **Run AOI on 2 photos**. The console says the mask is black and that the result is UNVALIDATED. It lists 7 candidates: D01 2, D02 5.
3. **Open issue review** shows one candidate at a time with its evidence sheet; Y / N / U as in any AOI run.

**What it found on D02** (the operator planted four defects: D1 polarity reversed, C7 missing, C2 lifted, U2 rotated 90 degrees). At the right part: C7 (as a "bridge") and D1 (as "missing"); the polarity check read D1 as correct. C2 and U2 were not flagged. **0 of 4 at the right class.** False alarms: D02 3, D01 2. Details, the reasons and the limits: `docs/AOI.md`, "YD example". Photos for a retake: `aoi/README.md`.

## The AOI walk, YD variant, diff mode (optional, simulated, two boards)

To use it, swap the `aoi:` block of `visual.yaml` for `aoi: {mode: diff, reference: D01}` (no `photos:`, so the photos are chosen by hand as below). The walk is `tests/e2e/aoi_diff_walk.py`, which does that swap in a temporary copy.

Run with `--boards 2` (the YD protocol's default is two). Everything below was typed and clicked in the real UI (Playwright WebKit, `tests/e2e/aoi_diff_walk.py`, 1500 x 900 and 1100 x 800; screenshots in `screenshots/yd/`). The analysis is real; only the instruments are simulated. HOST-ONLY, UNVALIDATED.

1. Open **Visual**. The bar shows an orange **Experimental diff** badge and the AOI photos view with two tiles, D01 and D02. The Assembly gate of D01 must be waived first (the walk does it through the gate override; do the same with "Override with a reason" in the Gate view, or walk Assembly first).
2. Click tile **D01** and choose `aoi/D01_top.jpg` (the clean unit, the reference). Click tile **D02** and choose `aoi/D02_top.jpg`. Each tile shows its thumbnail and the log says "Assigned to D01 (chosen for tile)".
3. Click **Run AOI on 2 photos**. The console reports, for D02: registration 204 inliers of 300 matches, reprojection RMS 1.47 px, glare masked on 35 % of the board, 12 regions "differs from the reference board". It takes about 3 s.
4. Click **Open issue review**. The first candidate is shown as a sheet: reference crop, the same crop of D02 registered, the difference map and where on the board it is. The text says D02 "differs from the reference board D01"; "Which board has the issue?" offers D02 and D01 (reference).
5. Click **Issue (Y)** without choosing a board: the page refuses ("Choose which board has the issue"). Choose **D02**, click **Issue (Y)**: the item is confirmed and D02's Visual state turns to attention (blocks Unpowered until Reworked). Click **No fault found** on the next item and **Unsure (U)** on the one after. Choose **D01 (reference)** for a later item and **Issue (Y)**: that issue blocks D01, not D02.
6. The journal records `mode: diff` on `aoi_photo`, `aoi_run` and `aoi_decision`, and `issue_board` on a decision.

What it found on this pair (D02 is the unit with visible flux residue): of 12 regions, the top ones cover the USB-C connector and CH343P area, the area next to the BOOT and RST switches and the regulator, and the passives beside the RGB LED, which is where the residue is visible in the photo; the others are the USB-C shell and the RF module can (parallax and glare on tall metal parts) and the antenna edge. The ranking does not separate residue from lighting: D02 is a glossier, differently lit photo, so most of the board scores as different and the residue regions are not clearly higher than the rest. The method surfaces the residue areas; it does not identify them as residue. Open the crops in `screenshots/yd/` and judge.

## The 3-minute walk, DevKitC-1 variant (simulated)

Use `--protocol examples/esp32-s3-board/devkitc-1.pcbv.yaml --boards 3`.

Every instruction below was typed and clicked in the real UI (Playwright WebKit, `tests/e2e/esp32_demo_walk.py`). The step you open starts where the previous step left the cursor, so click the board chip and the test row named below first. Start the first time with a fresh batch (the default data folder, or `--data-dir` to a new folder).

1. **Visual** (timeline, step 7). The cursor is on D01, V01. Type `y` and press Enter, 12 times: this step goes board by board (all four tests of D01, then D02, then D03). The checkbox "next board, same test" at the top right is off here; tick it to go test by test instead.
2. **Unpowered testing** (step 8). Click the chip **D01** and the row **U01**. This step goes test by test across the boards (the checkbox is on). Type, pressing Enter after each: `12`, `11`, `14` (U01 on D01, D02, D03), then `3.2`, `3.0` (U02 on D01, D02). Now type `0.3` for D03: it is below the 0.5 kOhm limit, which is a guess, so Enter does not accept it and a red REVIEW line appears. Click **Accept for review**: a guess limit sends the reading to review, it does not fail the board. Then type `Fluke 117` three times (U03).
3. **Decision gate**. Click the row **Gate after Tier 1 unpowered** in the left list. It sends D03 to Tier 2 with the reason (0.3 kOhm out of band) and marks Tier 2 skipped for D01 and D02. Click **Accept** on the D03 row.
4. **Powered testing** (step 9). Click **D01** and **P01**; type `62`, `3.31`, `4.98`, `y` (Enter after each). Click **D02** and **P01**; type `58`, `3.30`, `5.02`, `y`. Click **D03**: a blue "Gated" box says the power interlock holds it until its Tier 2 work is settled and Tier 1 is repeated (there is an "Override with a reason" button; do not use it).
5. **Program and function** (step 10). Click **D01**. Click **Flash the demo firmware (esptool)**, then **Continue** in the prompt; when the run ends, **Accept all clean for D01** appears: click it. Click **Read chip, MAC, flash and PSRAM size (esptool)**; when it ends, click the row **F03** in the test list (the button appears only once a row that has proposals is selected), then click **Accept all clean for D01**. Click **Run the serial self-test (serial.query)**, answer **Yes** to the LED question; when it ends, click the row **F07**, then click **Accept all clean for D01**.
6. **Report** (step 11). Marked SIMULATED. Generate the report files if you like.

The Project step shows the document names of the simulated ECAD bridge, which are not this design's; they are placeholders of the simulator and are ignored here.

## With a real board (UNVALIDATED)

Nothing here has run against a board. When you have a board (YD-ESP32-S3 or DevKitC-1, N16R8) plugged into the Mac, run from the repo root:

```
.venv/bin/polybench --protocol examples/esp32-s3-board/yd-esp32-s3.pcbv.yaml --port 8901     # or devkitc-1.pcbv.yaml
```

Things to know:

- **Cable and port (DevKitC-1).** The DevKitC-1 has two **Micro-USB** connectors (not USB-C). Use a data cable (not charge-only) in the one marked **UART**: it goes through the USB-UART bridge and serves both flashing and the serial self-test, because the demo firmware's `Serial` is on UART0 (`ARDUINO_USB_CDC_ON_BOOT=0`). The other connector, marked USB, is the chip's native USB: esptool can use it, the self-test cannot.
- The port is found by USB id; with more than one candidate, or if detection fails, add `port: /dev/cu.usbserial-XXXX` to each action's `params`, or set `POLYBENCH_ESP_PORT`.
- **BOOT is for esptool only.** If esptool cannot connect, hold BOOT, tap RESET, release BOOT, run again. That leaves the chip in the ROM download mode: press RESET alone afterwards. `serial.query` needs the firmware running; in download mode it gets no reply and says so.
- `serial.query` sends each command with a request id (`led #3`) and accepts only a reply that carries the same id, so a slow earlier reply is never taken for the next command. `timeout_s` is required on every step and is the time for one attempt: longer than the slowest command (the LED blink takes 4.8 s, so 12 s is set). A step is re-sent only when `retries` is set. This needs the firmware of this folder (older images do not echo the id).
- The LED is on GPIO38 for DevKitC-1 v1.1 and GPIO48 for v1.0 and for the YD-ESP32-S3: the YD `function.yaml` sends `led 48` (with the RGB jumper open the LED does not light, see above); for a DevKitC-1 v1.0 put `led 48` as the `send` of the LED step.
- Powered readings are typed from a USB power meter and a multimeter; nothing here drives a supply.
- Other module variants (N8, 1U, WROOM-2) will read a different flash or PSRAM size: that is why those limits are `guess` and name the module as what closes them.
- Report what happens: the first run closes the `closes_on` entries and the UNVALIDATED labels.
- Cancel: Abort ends an action with `done` and `cancelled`; `esptool.flash` stops esptool first, and a half-written flash must be flashed again.
- A run with no board shows one line ("No ESP32 board found on USB ...") in the console; the Python traceback of such an expected failure goes to `action_debug.log` in the run folder.

## Rebuild the firmware (HOST-ONLY)

```
~/.platformio/penv/bin/pio run -d examples/esp32-s3-board/firmware
cp examples/esp32-s3-board/firmware/.pio/build/esp32-s3-devkitc-1/firmware.factory.bin examples/esp32-s3-board/firmware/prebuilt/esp32s3-devkitc-demo.factory.bin
(cd examples/esp32-s3-board/firmware/prebuilt && shasum -a 256 esp32s3-devkitc-demo.factory.bin > MANIFEST.txt)
```

A rebuild gives a different hash; update `PROVENANCE.md` with it. Serial protocol (115200, UART0, one line in, one JSON line out): `selftest`, `wifi`, `led [gpio]`, each optionally followed by ` #<id>` which the reply echoes as `"id"`. A self-test line without id is also printed 1.5 s after boot.

## Release note

The factory image is committed in git for now. For the public release, ship it as a release asset and keep its SHA-256 in the repository (`PROVENANCE.md`, `prebuilt/MANIFEST.txt`), so that a 1 MB binary does not sit in the history. The image embeds Espressif binary libraries and LGPL arduino-esp32 code: see `firmware/PROVENANCE.md` for the licences (not yet audited).
