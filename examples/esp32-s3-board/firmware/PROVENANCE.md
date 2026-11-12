# Provenance of the prebuilt demo image

Image: `prebuilt/esp32s3-devkitc-demo.factory.bin` (967 488 bytes, merged bootloader + partition table + boot_app0 + application, write at 0x0).
SHA-256: `bc5d5d08e9454dfeafeb3dd1689e2144835b8ae2613c50877ada4777dd8f50d0` (also in `prebuilt/MANIFEST.txt` and as the `sha256` param of the flash action in `function.yaml`). `polybench doctor` does not check this image yet: its generic image check is on the `openocd-plugin` branch, and once that branch is merged the doctor checks the image against this SHA-256.

Built 2026-10-17 from `src/main.cpp` and `platformio.ini` in this folder, on macOS (arm64), with no source changes between the build and the commit.

## Toolchain (as resolved on the build machine)

| Part | Version |
|---|---|
| PlatformIO Core | 6.2.0 |
| Platform | pioarduino platform-espressif32 55.03.39 |
| Framework | arduino-esp32 3.3.9 |
| ESP-IDF inside it | v5.5.4 |
| Compiler | xtensa-esp-elf GCC 14.2.0 (crosstool-NG esp-14.2.0_20260121) |
| Image tool | esptool.py 5.3.0 (merges the factory image) |

Board `esp32-s3-devkitc-1`, `qio_opi` memory, 16 MB flash, `default_16MB.csv`, `ARDUINO_USB_CDC_ON_BOOT=0`.

## Build paths in the image (D4)

`platformio.ini` passes `-ffile-prefix-map` so the sketch and arduino-esp32 sources are recorded as `arduino-esp32/...`, `pio-core/...` and `project/...`. Checked on the built image with `strings`: no `/Users`, `/private`, `.platformio`, user name or clone path. Remaining path strings are `//IDF/components/...` and similar, which are Espressif's own prebuilt libraries (compiled by Espressif, with a neutral prefix); they are not under our control. The build is not claimed to be bit-reproducible: rebuilding on another machine gives a different hash, so the committed hash is the hash of this file, not of the source.

## Licences of what is inside (not a legal audit: UNVERIFIED, to be checked before a public release)

| Component | Licence | Note |
|---|---|---|
| Sketch `src/main.cpp` | the Polybench licence (open decision, see repo `LICENSE`) | |
| arduino-esp32 core and libraries | LGPL-2.1-or-later | Statically linked into the image. The LGPL asks that a user can relink: the sketch source and `platformio.ini` here, with the public arduino-esp32 3.3.9 release, are the means to do that |
| ESP-IDF v5.5.4 | Apache-2.0 | Includes its third-party parts under their own licences (for example lwIP, mbedTLS) |
| Espressif binary libraries (Wi-Fi, PHY, coexistence, ROM support) | Espressif's licence for binary blobs, distributed with ESP-IDF | Redistribution terms must be read from the ESP-IDF licence files before this image is published |

## Recommendation for the public release

Ship the image as a release asset with its SHA-256 written in the repository (this file and `MANIFEST.txt`), not as a binary in git history. The file stays in git for now.
