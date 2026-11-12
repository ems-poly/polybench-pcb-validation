# Concepts

A short tour of the words Polybench uses. Each has a one-line entry in the [Glossary](glossary.md).

## Steps and phases

Work is split into twelve **steps** in two **phases**. The step timeline across the top of the screen is the main navigation.

| Phase | Steps | Works on |
|---|---|---|
| Design | 1 Project, 2 Schematic analysis, 3 BOM and manufacturing, 4 Test plan | one design revision, no hardware |
| Bench | 5 Batch setup, 6 Assembly aid, 7 Visual, 8 Unpowered testing, 9 Powered testing, 10 Program and function, 11 Characterisation, 12 Report | a batch of built boards |

Bench steps are **step-first**: you do one step across all boards before moving to the next, and the boards sit side by side. Steps 2 and 3 are <span class="badge planned">Planned</span> and appear greyed as "later". See [The twelve steps](steps/index.md).

## Batches and boards

A **batch** is a set of boards built and tested together against one protocol file. You name it when you start the server (`--batch DEMO-B1`). Starting again with the same name resumes it.

Each **board** has a label made of the protocol's prefix and a number: `S01`, `S02` and so on. You record each board's serial in [Batch setup](steps/batch-setup.md). The board count can change only until the first result is recorded.

Real and simulated data never share a batch. A batch started with `--simulate` is labelled SIMULATED everywhere, including the report.

## The protocol file

The **protocol file** (`.pcbv.yaml`) describes one board design for Polybench: the BOM, the steps, the sections within each step, the tests, their limits, the probe points, the stop rules and the actions that read instruments. A top file can `include` fragments, typically one per step.

The protocol is the single source of truth at the bench. Polybench hashes it; if the file changes after a batch has started, the app bar shows PROTOCOL CHANGED.

See [Writing a test plan](guides/writing-a-test-plan.md) for a worked example and the [Protocol file reference](reference/protocol.md) for every key.

### The test plan as master <span class="badge preview">Preview</span> { #the-test-plan-as-master data-toc-label="The test plan as master" }

The **test plan** is a markdown document a person reviews. Attached with `test_plan: {file: ..., generate: true}`, it is the master for the Unpowered and Powered steps: Polybench generates their tests, limits, stop rules, Tier 2 sections and decision gate from the plan's tables when it starts. The protocol keeps what a plan cannot hold: the batch, the BOM, the instrument actions and the other steps. See [Step 4](steps/test-plan.md) and [Writing a test plan](guides/writing-a-test-plan.md).

## Tiers and the decision gate <span class="badge preview">Preview</span> { #tiers-and-the-decision-gate data-toc-label="Tiers and the decision gate" }

A generated plan splits testing into **Tier 1**, run on every board, and **Tier 2** deeper review, run only on boards that need it. After Tier 1, a **decision gate** reads each board's results and proposes Tier 2 when a reading is out of band, a stop rule tripped, or the board is an outlier against the batch. You accept or reject each proposal. With `hold power`, a board sent to Tier 2 cannot be powered until Tier 2 is done and Tier 1 has been repeated. See [Unpowered testing](steps/unpowered-testing.md#the-decision-gate).

## Tests

A **test** is one row of the plan: an ID (for example `U01`), a title, a kind, an optional unit, an optional limit, optional probe points and an optional stop rule. One test on one board is a **cell**.

| Kind | You enter | Example |
|---|---|---|
| `number` | a number, in the test's unit | `12.5` (kOhm) |
| `yn` | `y` or `n` | `y` |
| `choice` | one of the listed options, or its number | `1` for "clean" |
| `text` | free text | a serial number |
| `hex` | a hexadecimal value | `0x10810A05` |

## Limit classes

Every limit has a **class**. The class decides what a value outside the limit means.

| Class | Meaning | Outside the limit | Needs |
|---|---|---|---|
| `hard` | From a datasheet or a design calculation | **FAIL** | a source that is not a guess |
| `guess` | An estimate | flagged for **review**, never a FAIL | a source and `closes_on` |
| `tbr` | To be reviewed | no verdict; the value is kept and flagged | `closes_on` |
| `record` | Record only | no verdict | nothing |

`closes_on` says what will turn the estimate into a real limit, for example "measurement on the first 3 boards". The loader refuses a hard limit whose source is a guess or an estimate. A `yn` test with no limit expects `yes`.

Why it matters: a guessed limit that fails a good board wastes an hour of debugging. Polybench keeps the guess visible and lets the reading speak.

## Proposals and accepted values

A value typed on the keyboard is accepted when you press Enter. A reading from an action (an instrument, a programmer, a bus read) is an **instrument result**: it is recorded in the test as soon as the action delivers it, with its verdict and its source, and you do not accept it. You move on to the next board, or press **Redo** to measure that test again; the new value replaces the old one in the same attempt and the old one stays in the history. A value from voice or the AI assistant arrives as a **proposal**: it is not a result until you accept it (Enter), reject it, or **Accept all clean** for the board. A protocol can set an action to `record: propose` to make its readings proposals too. See [Results and anomalies](guides/results-and-anomalies.md).

A FAIL is never recorded by Enter alone. You use **Record FAIL** or Shift+Enter.

## Gates, stop rules and retests

- A **gate** blocks a step for a board until a required earlier step is complete without a FAIL. For example Unpowered testing requires Visual. You can override a gate with a typed reason, which is recorded and reported.
- A **stop rule** on a test blocks the board when a reading crosses a threshold, for example "VIN to GND below 1 kOhm: do not power this board". You acknowledge it with a reason.
- A **retest** sends a board back to a step with a cause and, optionally, the fix. It opens attempt 2. The first attempt stays in the journal and in the report.

## The journal

Every event is appended to one file per batch, `journal.jsonl`, and flushed to disk before the screen shows it as saved: accepted values, proposals, rejections, skips, retests, overrides, notes and action events. The app rebuilds its state from the journal on start. A crash or Ctrl+C loses nothing that was accepted. See [Journal and notes](guides/journal-and-notes.md).

## Readiness labels

Every report states how far its results can be trusted, per step and overall:

| Label | Meaning |
|---|---|
| HARDWARE-VALIDATED | The step has results from real hardware. |
| HOST-ONLY | The step's results came from a simulated run; nothing was measured on a board. |
| UNVALIDATED | The step has no results. |

The overall label is HOST-ONLY if any step is host-only, and PARTIAL when some steps have no results.

## Plug-ins and actions

An **action** is a Run button in a section of a step. It calls a **plug-in** (for example `n6705b.first_power` or `pcan.capture`) that talks to an instrument and proposes values to named tests. Plug-ins never decide pass or fail; the core does that from the limits. Every plug-in has a simulator path, used with `--simulate`. See [Plug-ins and instruments](guides/plugins.md).
