# Third-party notices

Polybench is MIT-licensed (see [LICENSE](LICENSE)). It includes or depends on the following. This is an engineering summary, not legal advice.

## Included in this repository

| Component | Where | Licence | Notice |
|---|---|---|---|
| Assembly Scanner (Fusion client, resolver, BOM import, camera pipeline) | `src/polybench/assembly_scanner/` (see `VENDORED.md` there) | MIT, Copyright (c) 2026 Assembly Scanner contributors | Vendored with changes from the standalone Assembly Scanner project, which is MIT. Its copyright line is kept here as the licence requires. |
| Assembly Inspector pipeline (AOI) | `src/polybench/aoi/` | MIT, Copyright (c) 2026 Assembly Scanner contributors | Ported from the standalone Assembly Inspector, same project and licence as above. |
| Voice pipeline | `src/polybench/voice/` | MIT, Copyright (c) 2026 Polystage | Ported from an earlier Polystage prototype. |
| IBM Plex Mono and IBM Plex Sans (woff2) | `src/polybench/static/vendor/fonts/` | SIL Open Font License 1.1, Copyright 2018 IBM Corp. | `OFL.txt` is shipped next to the fonts. The fonts are not sold on their own. |
| Cross-check data from shkuznetsov/YD-ESP32-S3 (`YD-ESP32-S3.kicad_mod`, converted from the maker's Altium file): board rectangle, header row spacing and pitch | `examples/esp32-s3-board/kicad/yd-esp32-s3/` (see `PROVENANCE.md` there) | MIT, Copyright (c) 2025 Alexander Kuznetsov | Used to cross-check the redraw's outline and header grid; no file or line is copied. The licence text is in the source repository (https://github.com/shkuznetsov/YD-ESP32-S3). The footprint file itself is not redistributed here. |
| KiCad redraw of the VCC-GND Studio YD-ESP32-S3 (schematic V1.4 and layout drawing as sources of facts) | `examples/esp32-s3-board/kicad/yd-esp32-s3/` | MIT (Polystage), with attribution to VCC-GND Studio (https://github.com/vcc-gnd/YD-ESP32-S3) for the connections, values and layout facts | Redrawn for the Polybench example; not VCC-GND's design files; not for manufacturing. VCC-GND's repository has no licence file. KiCad library symbols and footprints inside the files keep the KiCad library licence (CC-BY-SA 4.0 with the library exception). |

## Runtime dependencies (installed by pip, not copied here)

| Dependency | Needed by | Licence |
|---|---|---|
| fastapi, uvicorn, PyYAML, cantools | core | MIT, BSD-3-Clause, MIT, MIT |
| python-can | core (`pcan.*`) | **LGPL-3.0**. Used as an unmodified library installed by pip, which the LGPL allows from an MIT program; users can replace it. |
| opencv-python | extras `scanner`, `aoi`, `camera` | Apache-2.0; its wheels bundle FFmpeg and other libraries, mostly LGPL (`LICENSE-3RD-PARTY.txt` in the wheel) |
| pillow, numpy, openpyxl, zxing-cpp | extras `scanner`, `aoi` | MIT-CMU, BSD-3-Clause (with other permissive parts), MIT, Apache-2.0 |
| pillow-heif | extras `scanner`, `aoi` | BSD-3-Clause source. **Its binary wheels bundle x265 (GPL-2.0) and libheif/libde265 (LGPL-3.0)**, so the installed wheel as a whole is GPL-2.0. Polybench does not ship it; the user's pip installs it. |
| pyobjc-framework-AVFoundation | extra `scanner`, macOS | MIT |
| sounddevice, mlx-whisper | extra `voice` | MIT |
| kicad-python (kipy) | extra `kicad` | MIT |
| playwright, pytest, httpx | extra `dev` | Apache-2.0, MIT, BSD-3-Clause |

The `polystage-bench` library (MIT) is a separate, unpublished project that the `n6705b.*` actions import when it is installed. `esptool` (GPL-2.0-or-later) is used only as a separate command run in its own process by the optional ESP32 example; it is never imported into Polybench.

## Not included

Datasheets, vendor PDFs and instrument manuals are not distributed. Product and program names (KiCad, Fusion, Keysight, PEAK, Atmel, Espressif, OpenOCD) belong to their owners and are used only to say what Polybench works with.
