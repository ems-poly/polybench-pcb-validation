# Polybench

Polybench is a local, browser-based tool for validating a batch of prototype boards, from design review to bench bring-up. It runs on your laptop, opens in your browser and keeps every result on your disk.

You describe a board once, in a protocol file: its BOM, its tests, the limits and where each limit came from. Polybench then walks you and a batch of boards through the work, one step at a time, and writes a report at the end.

- **A test plan you keep in markdown.** Steps, limits and the source of each limit live in a file under version control. Only a `hard` limit can fail a board. The plan is a Preview feature: tested on the simulator, not yet used on a real batch.
- **Nothing accepted is lost.** Every value goes into an append-only journal and is flushed to disk before the screen shows it as saved.
- **Limits carry their source.** Only a limit tied to a datasheet or a design calculation can fail a board; a guess flags a value for review.
- **One thing in focus.** One test, one board, one input box. A FAIL needs its own key.
- **Instruments are kept safe.** Actions run in a separate process; abort cancels the action, then puts the instruments in their safe state.
- **See the part while you probe.** The test in focus selects its parts and nets in your ECAD program: KiCad 10 over its IPC API, or Autodesk Fusion. The Project page switches between them. Polybench only reads, selects and clears; it never edits a design.
- **An assistant that proposes.** An optional MCP server lets Claude Code read the bench state, propose values, ask questions and write a draft of the test plan. It has no tool to accept a result, record a FAIL, pass a gate or run an instrument, and the server refuses those requests. You read the diff and approve each plan change. Tested with Claude Code on a simulated batch; Codex should work through MCP and is unverified.
- **Everything stays local.** The server listens on 127.0.0.1 only.

Status: version 0.1.0, alpha. It is developed and tested on macOS. Linux runs the core and the simulator; this is not yet verified by the project. Windows is not supported.

What has and has not been shown on hardware:

- Tested against the real program: the KiCad 10.0.7 link (read-only, with some cases still open) and the Fusion link. The assistant was tested with Claude Code on a simulated batch.
- Tested on simulators only: the test-plan tiers and gate, voice (Preview), the camera scanner, the AOI modes, the ESP32-S3 demo and the OpenOCD plug-in. The OpenOCD flash plug-in has never flashed a board. A run of the demo on a real ESP32-S3 board and a first flash through an Atmel-ICE are planned and not done.
- AOI diff mode (no layout needed) is experimental and opt-in. It lists regions that differ from a reference board photo. It has no measured detection rate.

Each plug-in is labelled in the [documentation](site/docs/guides/plugins.md).

## Install

Polybench needs Python 3.12 or newer. The distribution is `polybench-pcb-validation`; the command is `polybench`.

```bash
pipx install polybench-pcb-validation          # or: uv tool install polybench-pcb-validation
polybench --help
```

The package is not on PyPI yet; these commands work once it is published. Until then, install from a source checkout as below. The built wheel installs with both tools in a scratch environment and `polybench selftest` passes from it.

Optional features are extras, for example `pipx install "polybench-pcb-validation[scanner,aoi]"`. Without an extra the app still starts; a feature that needs one says `install polybench-pcb-validation[extra] to use this`. The list is in [docs/PACKAGING.md](docs/PACKAGING.md) and on the [install page](site/docs/getting-started/install.md).

## Try it with no hardware

The example board is in the repository, not in the installed package, so clone it first. It needs no instrument:

```bash
git clone <REPO URL> polybench && cd polybench
python3.12 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/polybench selftest
.venv/bin/polybench --protocol examples/esp32-s3-board/yd-esp32-s3.pcbv.yaml --simulate
```

The browser opens on a simulated batch; every value is labelled SIMULATED. The example is an ESP32-S3 dev board (YD-ESP32-S3 or ESP32-S3-DevKitC-1): a visual step, a test plan, a decision gate, then flash and self-test actions. The walk-through is in [examples/esp32-s3-board/README.md](examples/esp32-s3-board/README.md). With a board plugged in, the function actions talk to the real chip; that path is unvalidated.

`polybench selftest` checks the whole chain on a built-in fixture and also works after `pipx install`, with no clone.

## Documentation

The user manual lives in [site/](site/) (MkDocs; `pip install -r site/requirements.txt && mkdocs build -f site/mkdocs.yml`). Start with [install](site/docs/getting-started/install.md), [`polybench doctor`](site/docs/getting-started/doctor.md) and [the first run](site/docs/getting-started/first-run.md). Reference documents are in [docs/](docs/): [test plan format](docs/TEST_PLAN_FORMAT.md), [plug-ins](docs/PLUGINS.md), [AOI](docs/AOI.md), [the AI connector](docs/AI_CONNECTOR_ARCHITECTURE.md).

## Privacy

Nothing leaves your machine unless you switch on a feature that says so. The one optional exception is an AI assistant you connect yourself. Polybench sends nothing; the assistant reads what its permission mode allows (`off`, `read` or `propose`) and sends it to that assistant's provider; see [Privacy and data](site/docs/guides/privacy.md).

## Contributing, support, security

[CONTRIBUTING.md](CONTRIBUTING.md) · [SUPPORT.md](SUPPORT.md) (best effort, one maintainer) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)

## Licence

MIT, see [LICENSE](LICENSE). Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
