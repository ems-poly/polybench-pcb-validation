# Command line

Polybench installs one command, `polybench`. Run it from the virtual environment, for example `.venv/bin/polybench`.

## polybench (serve)

Starts the server and opens the browser.

```bash
polybench [--protocol board.pcbv.yaml] [--batch NAME] [--simulate] [--port 8750] [--boards N]
          [--data-dir DIR] [--ecad NAME] [--operator NAME] [--no-browser]
```

| Option | Default | Does |
|---|---|---|
| `--protocol FILE` | the last project | The top protocol file. Without it the last project in the recent list is reopened; with no recent project the app starts on the welcome page |
| `--batch NAME` | `DESIGN_DATE` | Batch name; an existing batch is resumed |
| `--simulate` | off | Simulated actions and ECAD; data is labelled SIMULATED |
| `--port N` | 8750 | Port on 127.0.0.1; a busy port is reported, never freed |
| `--boards N` | from the protocol | Board count for a new batch |
| `--data-dir DIR` | `~/polybench-data` | Base folder for batches (also `POLYBENCH_DATA`) |
| `--ecad NAME` | `fusion` | ECAD bridge entry-point name |
| `--operator NAME` | none | Operator name recorded with results |
| `--no-browser` | off | Do not open the browser |

A protocol with lint errors is refused before the server starts. Ctrl+C stops the server; nothing accepted is lost.

## polybench lint

```bash
polybench lint board.pcbv.yaml
```

Prints each issue with file and line, then `N steps, N tests, N BOM lines, N errors, N warnings, hash ...`. Exit code 2 when there are errors.

## polybench plan

```bash
polybench plan lint   plan.md [--protocol board.pcbv.yaml]
polybench plan import plan.md [--protocol board.pcbv.yaml] [--out fragment.yaml]
```

Lints a [test plan](plan-format.md), or generates the protocol steps from it. Without `--protocol` the command looks for a `*.pcbv.yaml` next to the plan, or in a `polybench/` folder beside it, whose `test_plan` points at the plan; without one, designator and action checks are skipped. Exit code 2 when the plan has errors; `import` writes nothing then.

## polybench doctor

```bash
polybench doctor [--protocol FILE] [--expect-doc TEXT] [--n6705b-host IP] [--no-open] [--port N] [--json]
```

Read-only preflight. See [Check the bench with doctor](../getting-started/doctor.md). Exit code 1 when a line is red.

## polybench selftest

```bash
polybench selftest
```

Seven host-only checks, no hardware: the demo protocol loads, journal write and replay, limit classes, report rendering, the action registry, an action in a child process, the simulated ECAD bridge. Needs a source checkout (see [Install](../getting-started/install.md)).

## polybench aoi-derive

```bash
polybench aoi-derive --cam-zip board.zip --assembly assembly.yaml --design NAME --out-dir aoi/
```

Derives the AOI layout, the AOI protocol, a protocol fragment and `GAPS.txt` from a CAM package and the BOM fragment. See [Visual and AOI](../steps/visual-aoi.md#set-up-aoi-for-your-board).

## Environment variables

| Variable | Does |
|---|---|
| `POLYBENCH_DATA` | Base data folder (default `~/polybench-data`) |
| `POLYBENCH_STATE_DIR` | Where instrument plug-ins keep small state files (default `~/.cache/polybench`) |
| `POLYBENCH_N6705B_HOST` | Power analyzer address for doctor |
| `POLYBENCH_FUSION_URL` | Fusion MCP endpoint (default `http://127.0.0.1:27182/mcp`) |
| `POLYBENCH_FUSION_DOC` | Expected document name guard for the Fusion bridge |
| `POLYBENCH_OPENOCD` | Path to OpenOCD |
| `POLYBENCH_VOICE_CONFIG` | Voice settings file (default `~/.config/polybench/voice.json`) |
| `POLYBENCH_SIMULATE` | `1` makes the Fusion bridge simulate |
| `ASCAN_CONFIG_DIR` | Camera scanner settings folder |
