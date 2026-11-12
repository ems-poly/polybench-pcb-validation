# OpenOCD programmer plug-in

Actions `openocd.identify` and `openocd.flash`. They drive OpenOCD as a child process to read a chip's identity and to program and verify a firmware image over SWD. The plug-in proposes values; the operator accepts them. It never decides pass or fail.

Readiness labels used on this page: HARDWARE-VALIDATED (run on real hardware, with a record), HOST-ONLY (tested on the Mac with a fake OpenOCD or the simulator), UNVALIDATED (written, not run).

## Supported probes

| `probe:` | Interface script | USB restriction | Label |
|---|---|---|---|
| `atmel-ice` (default) | `interface/cmsis-dap.cfg` | `cmsis_dap_vid_pid 0x03eb 0x2141` | HARDWARE-VALIDATED for the probe-absent path only (OpenOCD 0.12 ran, the action said "No programmer found"). Identify and flash on a real target: UNVALIDATED. Command line: HOST-ONLY. |
| `cmsis-dap` (generic) | `interface/cmsis-dap.cfg` | none unless `vid_pid` is given | UNVALIDATED. Command line HOST-ONLY. |
| `stlink` | `interface/stlink.cfg`, `transport select hla_swd` | the script lists ST-LINK ids; `vid_pid` is refused | UNVALIDATED. Command line HOST-ONLY. |
| `jlink` | `interface/jlink.cfg`, `transport select swd` | `vid_pid` is refused | UNVALIDATED. Command line HOST-ONLY. |

Any other adapter: give `interface: <OpenOCD interface script>` or a whole `config:` file.

## Supported targets

| `target:` | Script | Device-id read | Label |
|---|---|---|---|
| `sam` (aliases `samd`, `saml`, `samc`, `samd21`, `samc21`, `saml21`) | `target/at91samdXX.cfg` | DSU DID, NVM user row, 128-bit serial number | HOST-ONLY. Flash and the id read have not run on silicon. |
| anything else, via `target_cfg: <script>` | your script | not supported yet | `openocd.flash` UNVALIDATED; `openocd.identify` stops with "not supported yet" |

`target_cfg` always wins. `target: stm32f4` together with `target_cfg: target/stm32f4x.cfg` programs with that script and marks the id read unsupported; `target: stm32f4` on its own (no script) is refused with "not supported yet".

Adding a family means one entry in `TARGETS` in `src/polybench/plugins/openocd.py` plus a read implementation.

## Parameters

```yaml
- id: flash
  title: Flash the image
  plugin: openocd.flash
  params:
    image: build/app.hex        # .hex or .elf (a .bin has no load address)
    probe: atmel-ice            # atmel-ice | cmsis-dap | stlink | jlink
    target: sam
    probe_serial: ""            # needed when more than one probe is connected
    speed_khz: 1000
    map: {flashed: F01, image_sha256: F02}
  proposes: [F01, F02]
```

`probe_serial`, `interface`, `target_cfg` and the image path are placed in OpenOCD `-c` commands, which are Tcl. A value containing any of `; [ ] $ " \ { }`, a control character or newline is refused with a message naming the parameter, before OpenOCD starts; a serial may not contain a space (a path may). This includes a serial read from USB: the message then says the probe's own serial is unusable and to set `probe_serial`. `interface` and `target_cfg` are run by OpenOCD as Tcl `script {...}`, so a `}` in them would inject commands; they are refused the same way.

Also: `vid_pid: [vid, pid]` (atmel-ice and cmsis-dap only), `target_cfg`, `interface`, `config` (one OpenOCD config file replacing interface and target), `openocd` (path to the executable; else `POLYBENCH_OPENOCD`, else `PATH`, else Homebrew), `timeout_s` (flash). With no `probe` and no `target` the command line is the one the earlier protocols used.

`openocd.identify` maps (`map:`) `probe_serial`, `device_id`, `flash_kb`, `bootprot`, `bodvdd_level`, `bodvdd_enabled`, `nvm_user_row`, `uid` to test ids. Words it did not read are logged, never guessed.

## Install

```sh
brew install open-ocd          # 0.12 or newer
openocd --version
polybench doctor --protocol <your protocol file> --no-open
```

The doctor checks OpenOCD and the probe only when the protocol has an `openocd.*` action.

## Wiring (SWD)

| Probe pin | Target | Note |
|---|---|---|
| SWDCLK (CLK) | SWCLK | |
| SWDIO (DIO) | SWDIO | |
| GND | GND | always |
| nRESET (RST) | RESET | recommended; some parts need it to attach after a bad image |
| VTref | target supply (I/O voltage) | the probe sets its logic levels from it; it does not power the target |

The target must be powered on its own. Use short leads and a common ground. Pin-outs differ per probe and per header: check the probe's own manual.

## Failure messages

| Message | Meaning | What to do |
|---|---|---|
| `openocd is not installed` | No executable found | `brew install open-ocd`, or set `POLYBENCH_OPENOCD` |
| `No programmer found ...` | OpenOCD sees no matching adapter | Plug the probe in, try another cable or port, close any vendor IDE or other OpenOCD holding it. Polybench never stops a process it did not start. |
| `... was found but cannot be opened` | Another program holds the probe | Close it, or unplug and replug the probe |
| `More than one <probe> is connected` | Several probes on USB | Set `probe_serial` in the action |
| `Target not responding on SWD ...` | No answer on the debug port | Power the target; check CLK, DIO, GND and RST contact; a dead supply and an open wire look the same |
| `The device is locked ...` | Security bit set | SAM targets: a chip erase is needed; the message prints the command. This erases the chip. Other targets: the message is generic (use the vendor's unlock procedure). |
| `An OpenOCD from an earlier run (...) is still running: pid N` | The core was killed mid-flash and its OpenOCD survived | See "A surviving OpenOCD" below |
| `probe_serial / image path contains a character that is not allowed` | A value has Tcl syntax in it | Remove the character |
| `Flash did not verify` | Program or verify failed | Read the last lines shown; reflash; check supply stability |
| `... (timed out after N s)` | OpenOCD hung | The child was stopped by the action; check the probe and cable |
| `The device-id / serial-number read is not supported yet` | `identify` on a non-SAM target | Use `openocd.flash` only, or add the family |
| `unknown probe` / `target ... is not supported yet` | Parameter typo | See the tables above |

## A surviving OpenOCD

If the Polybench core is killed hard (SIGKILL) while it programs, the OpenOCD child it started keeps running and holds the probe. Every run therefore writes `openocd.pid` (pid, start time) in its run folder while its OpenOCD runs, and removes it when OpenOCD has exited. The next `openocd.*` run and the safe-state action look in the sibling run folders and in `<data folder>/<batch>/runs/*/` for such files; for each pid still alive and still an OpenOCD (a reused pid is ignored) they report the pid, the command and the run, and say how to stop it: `ps -p <pid>`, then `kill <pid>` yourself once you are sure nothing is programming. Polybench never stops a process that this run did not start. The check only reads files and runs `ps`. HOST-ONLY: tested by killing a real core process with SIGKILL and finding the survivor from the next run.

## Contract

Simulator path (`simulate`) with realistic values, `selftest()`, `safe_state()`, run in the core's child process, proposals only, one OpenOCD per run, one controller per probe (a second probe needs its own `probe_serial`), and it stops only the OpenOCD it started (own process group, SIGTERM then SIGKILL). It never touches an OpenOCD started by anything else; one left behind by a killed core is reported, not stopped.

See also [../PLUGINS.md](../PLUGINS.md).
