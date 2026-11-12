# Troubleshooting

Start with `polybench doctor --protocol <your protocol>`: most problems show as a red or amber line with the fix under it. See [Check the bench with doctor](getting-started/doctor.md).

## Start-up

### "protocol has N error(s); fix them first"

The server refuses a protocol with lint errors. Run `polybench lint <file>` (and `polybench plan lint <plan>` for a generated plan); each error names its file and line. See [Protocol file: what lint checks](reference/protocol.md#what-lint-checks).

### Port 8750 is busy

Polybench says so and never stops another program. Close the other copy, or start with `--port 8751`.

### PROTOCOL CHANGED in the app bar

The protocol file changed after the batch started. Results are kept, but check the change was intended. Editing a generated test plan does not raise this; it adds a new plan load in step 4.

### `polybench selftest` fails with a TypeError on the demo protocol

The self-test reads `src/polybench/examples/demo/` from a source checkout. Install with `scripts/install.sh` or `pip install -e .` from the repository. See [Install](getting-started/install.md).

## During a session

### "SERVER NOT REACHABLE"

The server stopped (Ctrl+C, a crash or a closed terminal). Start it again with the same command and the same `--batch`. Nothing you accepted is lost.

### Enter does not record my value

The value is a FAIL or a review value. Press Shift+Enter or **Record FAIL**. This is deliberate: a FAIL is never recorded by accident.

### The step says "Gated"

An earlier step is not complete for this board, or it has a FAIL, or the power interlock holds the board. The banner says which. Finish the earlier step, retest the board, or press **Override with a reason...**.

### I entered a value on the wrong board or test

Go to the right cell and enter the value, or go to the wrong cell, type the correct value and press Enter twice to replace it. To leave a cell empty, use **Skip with reason...**. The first entry stays in the journal.

### An action hangs or looks wrong

Press **Abort** in the bottom bar. Polybench sends a cancel, waits for the plug-in and then puts the instruments in their safe state. The console shows each stage.

## ECAD program

### The ECAD program does not highlight

- The probe card says why under the test title.
- Check that the right document is the **active** tab, and press **Check again** in [Project](steps/project.md).
- Check the ECAD program's MCP server is enabled and reachable on 127.0.0.1.
- Manual entry always works; the highlight is a convenience.

### "Orientation not confirmed from design data"

The polarity card could not prove the orientation from the design data: the BOM line has no `polmark`, a footprint read failed, or a check disagreed. The reason is under the card. Check the silkscreen by eye. See [Assembly aid: polarity card](steps/assembly-aid.md#polarity-card).

## Programmer

- **"No programmer found":** the USB cable, the right port on the probe's cable, and that no other program (an IDE, another OpenOCD) holds the probe.
- **"Target not responding":** target power, and the SWD clock, data, reset and ground leads. The probe gives no target voltage through OpenOCD, so a missing supply and a missing wire look the same.
- **"Device locked":** the security bit is set; erase the chip with your vendor tool.
- **"Two probes connected":** set `probe_serial` in the action's params.

## CAN

- **"not free" or "channel not initialised":** another program holds the adapter; the message and doctor name it by process. Close it yourself; Polybench never stops another process.
- **No frames received:** node power and firmware image, the bitrate, CAN_H and CAN_L not swapped, the termination (fit 120 ohm at the adapter end when the node has none), and the IDs in the decode map.

## Power analyzer

- **Does not answer:** it is a network instrument; check it is on and reachable, wait a minute and press the action again. Or use a bench supply and a meter and type the readings.
- **Warns "in current limit":** the board draws more than the profile's limit. Switch off and investigate before raising any limit.

## Camera scanner

- **No image:** grant camera permission to the terminal that runs Polybench (macOS System Settings > Privacy & Security > Camera), choose the right **Camera**, and check no other app holds it.
- **Codes not decoded:** hold the label flat and closer, avoid glare, and check **Mirror**. Type the code or the MPN into the scan box instead; it takes the same path.

## AOI

- **Warning that the solder mask is not red:** the pipeline is tuned for red mask. Treat every flag, and every absence of flags, with extra care.
- **Photos land in "No board":** the file name had no board label. Drop photos on a board's tile instead.

## Voice

- **"not installed" in the Voice tab:** install the extra, `pip install -e '.[voice]'`, and fetch the speech model once as the settings show.
- **The microphone does not open:** give the terminal microphone permission, and keep the Polybench page visible: the microphone closes when the page is hidden.
- **"Default model ... not installed":** run the `ollama pull` command the settings show, or pick an installed model. Without Ollama, voice works with the grammar alone.
- **A value is not accepted by "accept":** it is a FAIL or review value. Say "accept fail" if that is what you mean.
- **Board and value run together** ("S20.2"): Polybench splits them against the batch's board count; say the board clearly, or "board two, 0.2 volts".
