# AOI in the Visual step

Status: version 0.1.0 (10 Nov 2026: layout mode runs on the YD example, see "YD example"). Ported from an earlier standalone Assembly Inspector prototype; the rule was to port the working prototype, not to rewrite it. Code: `src/polybench/aoi/`. Tests: `tests/aoi/` and `tests/test_aoi_polybench.py`. Everything is HOST-ONLY.

AOI is review assistance. It lists candidates; the operator's Y / N / Unsure decides. Nothing becomes a PASS because AOI found nothing.

## Flow

1. Visual step, mode bar **AOI photos** (16 Oct, later): one **tile per board** (S01..S10, a 5 x 2 grid on a narrow window) shows the photo count and up to three thumbnails. Drop photos on a tile, or click the tile to choose files: they go to that board whatever the file name says (journal `assigned_by: tile`). Dropping on the general area assigns by file name (`assigned_by: filename`), otherwise the photo lands in a "No board" card; the per-photo select journals `assigned_by: select`. A photo's thumbnail can be dragged onto another tile to move it (`aoi_photo_move`). Every photo has a thumbnail (HEIC is served as a converted JPEG preview), a click opens a full-size viewer (arrows move through the photos), the sharpest photo per board and side carries an **AOI used** badge (the same choice the run makes), and **Remove** (confirm, optional reason) moves the file to `<batch>/aoi/removed/`, journals `aoi_photo_removed` and keeps it out of the next run. The upload log lists each assignment, for example "Assigned to S03 (dropped on tile)". Original description: drag and drop photos (several per board). The board comes from the file name (`-S03`, `-B3`, the batch label), else it is chosen in the list; the side from `top`/`bottom` in the name. Each upload is stored under `<batch>/aoi/photos/` and journalled (`aoi_photo`). Unassigned photos are never used. Of several photos for one board and side the sharpest is analysed; the rest stay as stored evidence.
2. **Run AOI** (left panel button or the run button; beside it a disabled **Live capture** entry tagged *Coming (V2)*, see Planned below): the `aoi.run` action, a child process with live console and cancel (cancel is honoured at the next log line, between pipeline stages). Registration, protocol checks, defect stage (bridges including inter-part pairs, missing, lifted, peer anomaly across boards) and polarity run as in the PoC. Result and evidence go to the run folder `<batch>/runs/NNNN_*/`; the candidate list is journalled (`aoi_run`).
3. **Issue review** (mode bar): the PoC review page, embedded: one candidate at a time, the crop, the full-board locator, the same crop on the other boards, peer tags, Y / N / U. Each decision is journalled (`aoi_decision`) with the operator as source and the AOI value, confidence, note and evidence hash kept beside it.
4. **The AOI review is the Visual step.** The manual Yes/No checklist is gone from the step (archived; it returns later as an optional generic step codified from photos). For a protocol with an `aoi:` block a board's Visual state comes from the AOI review alone, and the left panel shows only the AOI section (Run AOI on all photos, Live capture, advanced actions) and the per-board AOI progress:
   - **empty**: the board has no AOI photo, or no AOI run covers it. The Unpowered gate says "no AOI photo for S01" or "no AOI run covers S01".
   - **partial**: items still undecided. Progress is decided / total, the items being the run's candidates plus the issues you added with **+ Issue**. Gate reason: "S01 has N AOI items not decided".
   - **done**: a photo, a covering run and every item decided (confirmed, not an issue or unsure). A covered board with no candidates is done.
   - **attention**: at least one confirmed issue that is not reworked. The gate to Unpowered blocks with "S01 has N confirmed AOI issues" until each is marked reworked (or the gate is overridden with a reason, as for any gate).
   - **Reworked**: a button on a confirmed issue in the checklist (optional note). It journals `aoi_reworked` (key, board, note; `on: false` takes it back), replays from the journal, and is cleared by a new decision on the same item. The issue stays confirmed in the report.
   - **Unsure** never blocks. It shows as a warning (the board's unsure count in the left panel and the checklist, the gate's `warnings`, and an open item "WARNING unsure" in the report).
   - **N** on every candidate that touches a test still gives a PASS proposal, and **Y** a FAIL, on any visual test the protocol happens to carry (a protocol without an `aoi:` block keeps its own test-based Visual step); with no visual tests nothing is recorded beyond the decision.
5. Report step: AOI section with run, photos, per-board counts, the issue list (decision, operator, AOI value, tests failed) and open items for undecided or unsure candidates.
6. Label Studio export stays an advanced action (`aoi.ls_export`): NOT PORTED as an action (stub); `ls_project`, `ls_export`, `ls_coords` are ported as modules.

## Diff mode (no layout), EXPERIMENTAL (17 Oct 2026)

Status: **experimental and opt-in** (default pending review): used only when a protocol sets `aoi: {mode: diff}` (the YD example used it until 10 Nov 2026 and now uses layout mode; swap one line to get it back, see "YD example"), labelled Experimental in the AOI bar, the review page, the report section and here. The layout-based AOI above stays the default and is unchanged. The diff code is self-contained (`diff.py`, `diffrun.py`, `privacy.py`; the hooks in `runner.py`, `engine.py` and the review page only branch on `mode`), so it can move to demo-only later. HOST-ONLY and UNVALIDATED. Code: `src/polybench/aoi/diff.py` (method), `diffrun.py` (run), `privacy.py` (EXIF). Tests: `tests/aoi/test_aoi_diff.py`, `tests/aoi/test_aoi_privacy.py`, walk `tests/e2e/aoi_diff_walk.py`. Verification table: `docs/AOI_DIFF_VERIFICATION.md`.

For a board with no recorded layout (a dev board, a clone, a board whose CAD you do not have). One photo is the reference board; every other board's photo is registered onto it and compared. It lists regions that **differ from the reference board**. It never says "defect".

```yaml
aoi: {mode: diff, reference: D01}     # optional: crop: [x0, y0, x1, y1] (fractions of the reference photo) instead of the automatic outline;
                                      # optional: threshold: 0.62
```

Same Visual step, tiles, photo assignment, review page, decisions, gate and report as the layout mode. Journal events carry `mode: diff` (`aoi_photo`, `aoi_run`, `aoi_decision`); a decision on a difference also carries `issue_board`.

Method (all of it colour independent; no red-mask assumption anywhere in this mode):

1. Photos are scaled to 1600 px long side. SIFT features on a CLAHE-equalised grey image, ratio-test matches, RANSAC homography (the board is planar), then a second guided pass that accepts looser matches agreeing with the first fit (spreads the inliers over the board), then ECC refinement and a smoothed, clipped dense-flow residual (max 6 px), each kept only if it raises the correlation inside the board.
2. Board outline: automatic, from edge density (a populated board is dense in edges, a mat is not), or the operator's `crop`. Outside the outline nothing is compared.
3. Registration quality is reported: matches, inliers, inlier ratio, reprojection RMS in px, ECC and flow result (run console, `aoi_run.registration`). **Refused** with a message starting "retake the photo:" when fewer than 40 inliers, an inlier ratio under 25 %, an RMS over 3 px, a degenerate warp, or a board covering under 8 % of the frame. Nothing is compared then.
4. Illumination: CLAHE, then the local mean (sigma 15 px) is removed and the image is scaled by its standard deviation. Compared on structure: the contrast-structure term of SSIM (window sigma 3 px), best of nine +-2 px shifts so a one or two pixel residual does not light every edge. Raw RGB is never compared.
5. Glare: pixels saturated (any channel at 250 or more) in either photo, grown by 4 px, are masked and the fraction of the board masked is reported. A masked pixel is never a candidate.
6. Candidates: smoothed difference above 0.62 (TBR, chosen on the demo pair and on a synthetic board, not tuned further), grouped, a region larger than 2 % of the frame is split at higher levels, ranked by excess area, at most 12. Each has an evidence sheet: reference crop | board crop (registered) | difference map | where on the board.
7. Decision: Issue (Y) / No fault (N) / Unsure (U), and for Y **which board has the issue** (the compared board or the reference). The gate follows the named board: an issue named on the reference blocks the reference's step, not the compared board's. Undecided items block, confirmed issues need Reworked, Unsure only warns, as in the layout mode.

What it cannot do:

- **With two boards it cannot tell which one is wrong.** A difference is a difference. The operator decides and names the board.
- Three or more boards: not built. The item carries the region in reference pixels and the compared board, which is what an odd-one-out across peers needs (regions that coincide on several boards point at the reference); the suggestion field is empty today.
- **Parallax.** Tall parts (USB-C shells, the module can, the RF shield) move against the board with the viewing angle. Two photos from different angles show them as differences even when nothing is wrong. Take the photos from the same top-down position.
- **Glare** hides what it covers; shiny shells and a glossy flux film both produce it. Large masked areas mean the photo should be retaken in softer light.
- **Lighting and exposure differences** between the photos raise the difference level everywhere; the structure measure is robust to a uniform change, not to a different light direction.
- It is not a substitute for the layout-based checks: it does not know what a part is, so it cannot check polarity, a missing part, a wrong value or a solder bridge. It reports that two pictures differ in a region.
- Not validated: no known-fault board has been run, so there is no detection rate and no false-alarm rate. The 0.62 threshold and the registration limits are TBR choices.
- A photo is stored without any EXIF (see below), so the shooting data is not available to the method or the report.

Privacy (all AOI photo import, both modes): every photo stored through `/api/aoi/photos` has its metadata removed, GPS included. A JPEG with normal orientation is cleaned losslessly (metadata segments dropped, pixels untouched); a rotated JPEG is turned upright and rewritten; HEIC and PNG are written as a lossless PNG (same decoded pixels, so results do not change). A file that is not an image container is stored as received. The photos in `examples/esp32-s3-board/aoi/` were converted the same way.

## YD example: layout mode on a KiCad redraw (10 Nov 2026)

HOST-ONLY and UNVALIDATED. Two phone photos of the VCC-GND YD-ESP32-S3 (D01 clean reference, D02 with four defects planted by the operator), not a live camera: **this is not HARDWARE-VALIDATED and must not be called so.** The example (`examples/esp32-s3-board/visual.yaml`) uses layout mode; the experimental diff mode is still there: replace the `aoi:` block with `aoi: {mode: diff, reference: D01, photos: {D01: aoi/D01_top.jpg, D02: aoi/D02_top.jpg}}` (walk `tests/e2e/aoi_diff_walk.py` still runs it).

**Inputs.** `aoi/layout/build_layout.py` rebuilds everything from `kicad/yd-esp32-s3/` with kicad-cli (KiCad 10): front and back pick-and-place, F.Paste, F.Mask and Edge.Cuts Gerbers, packed as `yd-esp32-s3_cam.zip` under the names the layout guard reads, then `aoi-derive`, then `polarity_spec.py` (pads and nets read from the board file: `polarity.json`, `netlist.json`, the protocol's rows). The zip is **not a released CAM package**: it is made from the redraw, which is not a manufacturing file, so the guard compares the layout with the same positions it was derived from (0.02 mm) and proves nothing about the real board. The redraw's placement is good to about 0.3 mm (its PROVENANCE) and the guard was not weakened. Generated files are committed (about 60 kB).

**What had to change in the core, all generic and tested** (`tests/aoi/test_aoi_yd_layout.py`): the outline reader chained KiCad's separate Edge.Cuts segments into a diagonal; the pad reader did not know KiCad's rounded-rectangle apertures; derive did not know KiCad footprint names (body sizes); the outline and refinement steps looked for a red mask (`registration.board_colour: dark`, above); the layout outline is the 57.15 mm board and the photographed board has a 6.24 mm antenna tongue (`registration.overhang_mm`); off-plane parts are left out of the photo-to-photo fit (`defects.tall`); each polarity part's view is moved onto the reference board's view before the detectors read it (`locate`); and the polarity validation takes `min_decided` from the spec, because the default of 3 boards can never be met by a two-photo example (the 180 degree flip test itself is unchanged).

**What it found on D02** (run of 10 Nov 2026 on the committed photos; the operator's marked image is internal; table of designators in the test). Verdict at the right designator **and** the right class:

| Planted defect | What AOI said | Verdict |
|---|---|---|
| D1 polarity reversed | `AOI-MISSING-D1` (bare mask between the pads, 58 %); the polarity check read D1 as correct (`AOI-POL-D1` pass, band score 41 on the expected side) | found at the right part, **wrong class**; the polarity reading is a **false pass** |
| C7 missing | `AOI-BRIDGE-C7.1-R5.1` (bright bare pads read as solder) | found at the right part, **wrong class** |
| C2 lifted | nothing | **missed** |
| U2 rotated 90 degrees | `AOI-POL-U2` at review, "no clear marking", not queued (confidence 0.2) | **missed** (an unqueued note at the right part) |

Score on the success target (4 of 4 at the right part): **2 of 4 at the right part, 0 of 4 at the right class.** False alarms (queued items naming none of the four): **D02 3** (`BRIDGE-R4.2-TX.2`, `BRIDGE-R10.1-R9.1`, `MISSING-C5`), **D01 2** (`MISSING-C8`, `MISSING-R9`); no threshold is set (TBR). Not queued: with two boards every part "differs from the batch median" (a presence-row note per part, as for any two-board batch); peer comparison needs three or more boards. IC rotation is covered only by the pin 1 marking (polarity), not by geometry: a 3 x 3 QFN looks the same turned by 90 degrees.

**Why, and whether it is the photos (R5).** The method, not the pixel count: the same run on the 4032 px originals (not committed) gave the same items and readings. The causes seen in the evidence sheets: the mask is black, so the red-mask thresholds are uncalibrated; D02 is lit differently and its flux residue is glossy around C7 and D1; the diode band is three stripes about 0.1 mm wide. Better photos may help and cannot be promised to: see `aoi/README.md` (photo protocol). Candidate method work, none done: a part-local registration for the whole defect stage (D02's pad refinement is refused, 94 px), a black-mask calibration, and a polarity check that finds the body first.

## Planned

- **Live capture** (Planned, V2, roadmap T6.5): capture a frame from a USB camera or microscope and run AOI on it; later a live overlay. The entry exists in the Visual left panel and in the photos view as a disabled button with that note. There is no camera code and no request to any device behind it.
- The manual Visual checklist returns later as an optional generic step, codified from photos.

## Rules from the 16 Oct AOI performance review

- Decisions count only for the run they were made on, or when a later run's evidence (crop with pad overlay) is byte-identical. A re-run starts undecided otherwise.
- Polarity fails closed: undecided polarity says "look by eye" and never becomes a pass; polarity dismissals are not turned into pass proposals.
- Lift is review only: flag status is forced to `review`, the page says the 0.5 mm threshold sits inside the board-to-board spread (median 0.42 mm). An operator-confirmed lift still fails the test (the operator saw it).
- 45 degree parts (pair frame) and the DNF absence check use the PoC's code paths unchanged; the PoC lacks missing/lift checks for parts drawn with polygon pads (for example C18, C19, Y1, J2 on the reference board). Polybench does not add them.
- The pipeline assumes a red solder mask. The first photo's red-mask fraction is measured (the reference board measured 0.32 to 0.36); below 0.15 a warning is shown in the AOI view, the console and the run record.
- Bridge evidence boxes: test `tests/aoi/test_aoi_bridge_evidence.py` asserts every bridge evidence box contains both pad shapes (the e7c4451 stale-pad bug).

## Protocol block

```yaml
aoi:
  protocol: aoi_protocol.json   # polystage.aoi_protocol/0, relative to the file holding this block
  layout: layout.json           # recorded layout (parts, inspection polygons, outline)
  cam_zip: /abs/path/board.zip  # released CAM package; the layout is verified against its PnP before any inspection
  photos: {D01: aoi/D01_top.jpg, D02: {top: aoi/D02_top.jpg}}   # optional: photos that load into the batch by themselves (below)
```
Paths in the `aoi:` block are relative to the file that holds it; paths inside `aoi_protocol.json` (`defects.netlist`, `polarity.spec`) are relative to that JSON file.

**Photos named by the protocol (`photos`, 10 Nov 2026).** When a batch has never had an AOI photo (no `aoi_photo`, `aoi_photo_move` or `aoi_photo_removed` event) and a named board exists in it, the app copies each file into `<batch>/aoi/photos/` (metadata removed, as for an upload) and journals `aoi_photo` with `assigned_by: protocol`. It happens when the state is first read, in any mode, simulated or not. The operator's tiles, drops, moves and removals always win: a batch that had photos, even if all were removed, is never seeded again. A board that is not in the batch, or a file that is missing or not a photo, is skipped and nothing else changes. Code `src/polybench/aoi/seed.py`, tests `tests/aoi/test_aoi_yd_layout.py`, walk `tests/e2e/aoi_layout_walk.py`. HOST-ONLY.

**Registration for a board that is not red (`registration` in `aoi_protocol.json`).** The outline detector, the pad refinement and the missing check assume a red solder mask. `registration: {board_colour: dark, overhang_mm: {right: 6.24}}` selects a black-mask rule (board = the dark region on the mat; mask = pixels with HSV value under 60) and says that the photographed board runs 6.24 mm beyond the layout outline on the right (the YD's antenna tongue). `defects.tall` lists parts off the board plane; with `registration.fit_without_tall: true` their features are left out of the photo-to-photo fit (off by default, so existing protocols register as before). The polarity spec may carry `min_decided` (default 3) and each detector `locate_mm` (off unless set; the YD spec sets 1.2): the part's view is first moved onto the reference board's view of the same part by phase correlation. The dark-mask thresholds were **not** calibrated: a run on a dark board says so in its notes.
The AOI protocol may carry `defects: {fine_pitch_ics, tall, netlist}` and `polarity: {spec}` (both were constants of one board in the prototype). Without a polarity spec polarity is reported as NOT assessed; without a netlist every close pad pair has an unknown net.

`polybench aoi-derive --cam-zip Z --assembly assembly.yaml --design NAME --out-dir D` derives layout, protocol, fragment and `GAPS.txt` from a CAM zip and the BOM fragment.

## Journal events (new types only)

`aoi_photo {board, side, name, file, sha256, size, source, assigned_by}` (`assigned_by` is new: tile, filename, select, protocol or unassigned; older lines have none), `aoi_photo_drop {file}` (old, file stays in place), `aoi_photo_move {file, from, to, name, assigned_by}`, `aoi_photo_removed {file, name, board, side, sha256, reason, moved_to}`, `aoi_run {run, result, dir, items, photos, counts, notes, synthetic}`, `aoi_decision {key, board, result_id, decision, note, aoi, run, tests, fail_tests, clear_tests, attempt}`. A journal without them replays unchanged.

## PoC feature table

| PoC feature | Polybench |
|---|---|
| photo_map (B1..B5 names, map.json) | changed: assignment from file name or choice in the web view, stored in the journal; map.json not used |
| layout from live Fusion | not ported: recorded layout only (derived from CAM) |
| layout_guard (layout vs CAM PnP) | ported |
| alignment, vision, batch (registration, exposure, peer compare) | ported |
| protocol_run, protocol_logic (rows, ceilings, presence) | ported; the CLI stays for tests |
| defect stage (bridges, missing, lift, anomaly), pads, board_view | ported; board constants moved to protocol data; lift review only |
| polarity (band, corner, flip validation) | ported; needs a polarity spec per design |
| review_merge, review page, locator, peer crops | ported and embedded; decisions in the journal instead of a decisions file; Export writes the PoC's reviewed JSON |
| review_server (own HTTP server, browser launch) | not used by the app; kept only for the ported tests |
| decisions file keyed by candidate id across runs | changed: per run, or identical evidence |
| `--exclude-confirmed` peers | ported in the pipeline, not wired to the journal yet |
| layout from a KiCad board (10 Nov, new) | `examples/esp32-s3-board/aoi/layout/build_layout.py`: kicad-cli exports, then `aoi-derive`; see "YD example" |
| diff mode (17 Oct, new, not in the PoC) | layout-free board-to-board difference, see the Diff mode section |
| ls_export / ls_project / ls_coords | modules ported; the action is a stub (needs a Label Studio URL and token) |
| protocol_gui (PySide window), Fusion MCP reader, photo_trial, main | not ported (desktop app, not needed) |
| defect_eval (scoring against labels) | ported for the tests |
