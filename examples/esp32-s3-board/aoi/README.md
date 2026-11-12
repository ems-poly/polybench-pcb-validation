# AOI photos of the YD-ESP32-S3 demo

`D01_top.jpg` is a clean unit and is the reference board. `D02_top.jpg` is a second unit with visible flux residue around the USB-C connectors and some passives, taken at a slightly different angle. Both are top views, converted from the phone's HEIC originals to JPEG with the long side at 2000 px; **all EXIF, including GPS, was removed** (`polybench.aoi.privacy.strip_metadata`; the tests check the committed files). The originals are not in this repository.

How they were taken (as they are, not as a model): handheld phone photos on a teal mat, daylight-type light with glare on the metal shells.

What a good photo needs for the experimental diff mode (AOI docs, "Diff mode"):

- **Flat, even light**: no hard shadows, no glare on connectors or the module can. Saturated pixels are masked and reported, and a large masked share means retake.
- **The same distance** for every board, with the board filling most of the frame.
- **Top-down**: the camera straight above the board. Tall parts (USB-C shells, the module can) shift with the angle (parallax) and show up as differences when the photos differ in angle.
- The same side up, in focus, nothing else on the board.

## Layout mode (the example's default, 10 Nov 2026)

`layout/` holds the layout derived from the KiCad redraw (`layout/build_layout.py` rebuilds it; `docs/AOI.md`, "YD example"). `visual.yaml` names these two photos, so they load into the batch by themselves. D02 has four planted defects (D1 polarity, C7 missing, C2 lifted, U2 rotated 90 degrees); the table of what AOI found, with its limits, is in `docs/AOI.md`. HOST-ONLY.

## Photo protocol for a retake (both boards, same session)

Not a promise that the result improves: the same run on the full 4032 px originals gave the same items, so the picture size is not the limit. These are the conditions the checks assume.

1. **Top-down**: the phone parallel to the board, centred over it (a tripod or a fixed jig; the same position for D01 and D02). Tall parts (USB-C shells, module can) shift with the angle.
2. **Same distance and framing for every board**, the whole board with its antenna in the frame and 5 to 10 % margin; the board on the plain mat. Then one **close-up of the left third** (USB-C ports, diodes, CH343P, the 0402 parts) from the same top-down position, about 60 px/mm or better, for the part-level checks.
3. **Diffuse light** (a light tent, or two soft lights from both sides, no flash, no window beside the board): no hard shadows, no glare on the shells, and **the same light for D01 and D02**. D02's glossy flux residue around C7 and D1 reads as bright bare pads in the current photo.
4. Focus on the board plane (tap on a pad row), exposure locked, HEIC or JPEG (the app removes EXIF), same side up, nothing else on the board. Clean the flux residue before the photo only if the residue is not what is being tested.
