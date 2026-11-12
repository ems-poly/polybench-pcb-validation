# Check the bench with doctor

`polybench doctor` is a read-only preflight. Run it before every bench session, after plugging in the instruments. It tells you, line by line, what is ready, what is optional and what must be fixed, and how.

Why: most lost bench time is the set-up, not the boards. Five seconds of doctor saves the hunt for a held adapter or a missing image.

## Run it

```bash
.venv/bin/polybench doctor --protocol path/to/board.pcbv.yaml
```

- With `--protocol` it also lints the protocol and checks the firmware images, image hashes and DBC files the protocol names.
- The checks follow the protocol: a programmer, CAN adapter, power analyzer or ECAD bridge is checked only if an action or a capability of the protocol uses it. With no protocol, doctor lists what is installed and raises no red line for hardware.
- `--expect-doc TEXT` (optional, no default) warns if the document open in the ECAD program does not contain `TEXT` in its name.

## Read the result

Each line is green, amber or red.

| Colour | Tag | Meaning |
|---|---|---|
| Green | `OK` | Ready. |
| Amber | `WARN` | Optional, or you will type the values by hand (for example the power analyzer does not answer, so you use a bench supply and a meter). |
| Red | `FAIL` | A required item is missing. The line under it says how to fix it. |

Example, for a protocol that programs a target over SWD and uses the power analyzer, on a laptop with no programmer plugged in. The protocol declares the power analyzer optional:

```text
[OK  ] Python                   3.12.3 in venv /path/to/.venv
[OK  ] package python-can       4.6.1
[OK  ] package cantools         45.0.0
[WARN] package polystage-bench  not installed (optional capability)
                                -> needed for the N6705B actions: scripts/install.sh
[OK  ] action plug-ins          the protocol's 4 are found and loadable: n6705b.measure, openocd.flash, ...
[INFO] ECAD bridges             altium, fusion, kicad
[OK  ] OpenOCD                  Open On-Chip Debugger 0.12.0 at /opt/homebrew/bin/openocd; probes used: Atmel-ICE (CMSIS-DAP)
[FAIL] probe Atmel-ICE          not enumerated (USB vendor 03eb)
                                -> plug the probe in by USB; on the 10-pin cable use the SAM port
[INFO] N6705B power analyzer    used by the protocol; not queried (opt-in)
                                -> add --n6705b-host IP or --check-n6705b to send one *IDN? (read only)
[INFO] data folder              ~/polybench-data does not exist yet; Polybench creates it when the first batch starts
[OK  ] port 8750                free
[OK  ] protocol lint            board.pcbv.yaml: 8 steps, 22 tests, 0 errors, 0 warnings
[OK  ] firmware images          the protocol flashes nothing
[OK  ] DBC files                the protocol uses none

doctor: 8 ok, 3 info, 1 warn, 1 fail. NOT READY: fix the red lines. (read-only: nothing was written to a device)
```

The exit code is 1 when any line is red, so a script can stop on it.

!!! note "Only what the protocol uses is checked"
    The demo protocol needs no programmer, CAN adapter or power analyzer, so none of those lines appear and it reports READY on a bare laptop. A capability the protocol declares `optional: true` turns every red line that depends on it (package, plug-in, OpenOCD, probe, adapter, images, DBCs, profile files) into amber.

## What is checked

| Check | Runs when | Red when | Amber when |
|---|---|---|---|
| Python, venv, packages | always | Python older than 3.12; a package a used action needs is missing | not in a venv; the package belongs to an optional capability |
| Action plug-ins, ECAD bridges | always | a plug-in the protocol names is not registered or does not load | the plug-in belongs to an optional capability. The bridge list is an INFO line |
| OpenOCD and probe | `openocd.*` actions; one line per probe | not installed; probe not on USB; the action's `vid_pid` (else the preset's id) not present | older than 0.12; two probes; a named `probe_serial` absent; optional programmer |
| PCAN adapter | `pcan.*` actions | not on USB; held by another process (named by PID and script, never touched); or opening failed | optional bus adapter |
| ECAD bridge | an `ecad` capability, or `--ecad` | | not reachable, no document, or a document without the `--expect-doc` text |
| N6705B | `n6705b.*` actions and `--n6705b-host IP` or `--check-n6705b`; otherwise one INFO line | | no answer to one `*IDN?` on TCP port 5025 within 3 s; outputs are not touched |
| Protocol, firmware images, DBCs | `--protocol` | lint errors; an `image` file missing (any plug-in); an image hash that differs from the protocol (`image_sha256` test or `sha256` param) or from `MANIFEST.txt`; a DBC unreadable or a signal missing | lint warnings; an image not listed in a manifest; the same faults on an optional capability |
| Data folder, port | always | the folder exists and is not writable | port in use. A missing data folder is INFO |

## What doctor never does

Nothing is written to a device, no CAN frame is sent, no output is switched and no process is signalled. It creates and writes no file: the data folder is tested with the operating system's permission check, and a folder that does not exist yet is reported, not created. With a free PCAN adapter the check opens it in listen-only mode (no acknowledge, nothing transmitted) and closes it at once; `--no-open` skips that.

## Options

| Option | Default | Does |
|---|---|---|
| `--protocol FILE` | none | lint the protocol; check images, hashes and DBCs |
| `--expect-doc TEXT` | none | warn if the open ECAD document name lacks `TEXT` |
| `--ecad fusion\|kicad` | `POLYBENCH_ECAD`, else fusion | which ECAD bridge to check |
| `--data-dir DIR` | `POLYBENCH_DATA`, else `~/polybench-data` | data folder to check; never created or written |
| `--check-n6705b` | off | send one `*IDN?` to the host from `POLYBENCH_N6705B_HOST` or `instruments.toml` |
| `--n6705b-host IP` | none | query the power analyzer at this address (one read-only `*IDN?`); without it or `--check-n6705b` it is not queried |
| `--no-open` | off | do not open the CAN adapter to prove it is free |
| `--port N` | 8750 | the port the app will use |
| `--json` | off | machine-readable output |

The power analyzer address can also come from the `POLYBENCH_N6705B_HOST` environment variable.

## Next

[First run with the demo](first-run.md). When a line stays red, see [Troubleshooting](../troubleshooting.md).
