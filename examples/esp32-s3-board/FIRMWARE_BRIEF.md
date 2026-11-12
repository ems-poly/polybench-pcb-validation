# Firmware brief: ESP32-S3 demo (worked example of the `polybench-test-firmware` skill)

Readiness: **HOST-ONLY.** Written from `function.yaml` (YD variant) and `firmware/src/main.cpp`; the commands, reply types, keys, test IDs and timeouts are checked against both by `tests/test_firmware_brief.py`. No board has run this firmware through Polybench (UNVALIDATED on hardware). The brief and the checked-in firmware agree; the brief was derived from the step, then compared with the code, not the other way round.

## 1. Purpose

Serve the step **Program and function** (`function.yaml`): sections `program` (action `flash`, tests F01 F02), `identify` (action `identify`, F03, F04, F05, F06, read by the ROM bootloader, no firmware involved) and `selftest` (action `selftest`, tests F07, F08, F10, F11, F12).

## 2. Link

| Item | Value |
|---|---|
| Port | The UART bridge connector (USB1, CH343P on the YD board). The firmware `Serial` is UART0; the native USB port does not carry it. |
| Baud | 115200 (the `serial.query` default) |
| Settle | 1.5 s after the port opens (default `settle_s`); the firmware waits 1.5 s after boot, then prints one `selftest` line with no id |
| Line ending | `\n`; the command arrives as `<command> #<id>` |
| State | Firmware running. Not in ROM download mode (BOOT held at reset): press RESET alone first. |

## 3. Commands

| Command | Reply `type` | Keys to test IDs | Timeout per attempt | Retries | Side effect |
|---|---|---|---|---|---|
| `selftest` | `selftest` | `flash_mb` to F07, `psram_mb` to F08, `mac` to F11 | 6 s | 1 | none |
| `led 48` | `led` | none mapped; operator answers F12 | 12 s | 0 | blinks the LED on GPIO48 red, green, blue four times, about 4.8 s, then off |
| `wifi` | `wifi` | `count` to F10 | 15 s | 0 | station mode, one scan, results discarded |

- Any other command: `{"type":"error","text":"unknown command","id":<id>}`.
- The 12 s on `led 48` is more than twice the 4.8 s blink (the blink time is read from the delay values in `main.cpp`, HOST-ONLY). The 15 s on `wifi` is an estimate for a scan, not measured (UNVALIDATED).
- `led <gpio>` takes the pin at run time. The default pin is the build flag `LED_GPIO_DEFAULT`; the YD protocol sends 48 explicitly.

## 4. Reply schema

`selftest`: `{"type":"selftest","fw":str,"chip":str,"revision":int,"cores":int,"mac":"aa:bb:cc:dd:ee:ff","flash_mb":int,"psram_mb":int,"free_heap_kb":int,"id":int}`

| Key | Unit | Test | Class in the protocol |
|---|---|---|---|
| `flash_mb` | MB | F07 | guess, 16 to 16, closes on the module variant on the shield |
| `psram_mb` | MB | F08 | guess, 8 to 8, closes on the module variant on the shield |
| `mac` | text | F11 | record |

`wifi`: `{"type":"wifi","count":int,"id":int}`; `count` to F10 (guess, min 1, closes on a place with a 2.4 GHz access point).

`led`: `{"type":"led","gpio":int,"id":int}`. Sent after the blink finishes.

Other keys (`fw`, `chip`, `revision`, `cores`, `free_heap_kb`) map to no test; they are for the operator reading the raw reply.

## 5. Operator question

After `led 48` Polybench asks: did the RGB LED blink red, green, blue? The answer is proposed for F12 (record, no verdict). The firmware reports nothing about the answer. On the operator's boards the RGB solder jumper is open (stated by the operator, not measured), so No is the expected answer.

## 6. Flash

Action `flash` (`esptool.flash`): the merged factory image `firmware/prebuilt/esp32s3-devkitc-demo.factory.bin` at offset `0x0`. Polybench refuses to flash when the file's SHA-256 differs from the protocol's `sha256`. A rebuild changes the hash (the build is not bit-reproducible), so after a rebuild the operator updates `sha256` in `function.yaml` and `prebuilt/MANIFEST.txt`; you give them the new value from `shasum -a 256`.

## 7. Not covered

- The RGB LED cannot be seen to light while the RGB jumper is open; F12 only records.
- No test reads the strapping pins, the USB-JTAG path or the native USB port.
- F05, F06, F07 and F08 hold until the variant printed on the shield is known (their `closes_on`).

## 8. Readiness

| Claim | Label | Evidence |
|---|---|---|
| Brief agrees with `function.yaml` and `main.cpp` | HOST-ONLY | `tests/test_firmware_brief.py` |
| Firmware builds and answers the commands on a board | UNVALIDATED | no board run through Polybench |
| Timeouts hold on a board | UNVALIDATED | blink time from the source; Wi-Fi scan time estimated |
