# Writing a test plan

This guide turns a board into something Polybench can run, using the shipped DEMO-1 board as the worked example. You write two files:

| File | Holds | Read by |
|---|---|---|
| **Test plan** (markdown, `plan_format: 1`) <span class="badge preview">Preview</span> | the Unpowered and Powered tests: limits with sources, probe points, stop rules, Tier 2 and the decision gate, plus your prose | people, in any markdown editor; Polybench generates tests from it |
| **Protocol file** (`.pcbv.yaml`) | the batch, the BOM, the capabilities, the instrument actions, and the Assembly, Visual, Function and Characterisation steps | Polybench |

The test plan is the master for the steps it defines. A protocol without a generated plan still works: all tests are then written in the protocol, as in `src/polybench/examples/demo/`.

The complete example is in the repository: `site/examples/demo-docs/DEMO-1_Test_Plan.md` and `demo-docs.pcbv.yaml`. The column rules are in the [Test plan format reference](../reference/plan-format.md).

## 1. Start from the questions, not the format

For each test write down:

1. **What must be true** (VIN is not shorted to ground; the 5 V rail is between 4.85 and 5.15 V).
2. **Where the number comes from**: a datasheet page and table, a design calculation, or "we do not know yet".
3. **What would stop you** going further (VIN below 1 kOhm: do not power).
4. **How the value is obtained**: typed from a meter, spoken, or read by an action.

The second answer decides the limit class: a datasheet page or calculation is `hard`; an estimate is `guess` and needs what closes it; a number nobody has reviewed is `tbr`; a value you only want recorded is `record`. See [Concepts: limit classes](../concepts.md#limit-classes).

## 1a. Put each test in the right step

Each step answers one question, and a board moves on only when the answer is yes. Keep a test in the step whose question it answers.

| Step | Question | Belongs here | Does not belong here |
|---|---|---|---|
| Unpowered | Is it safe to apply power? | Resistance, continuity and diode checks on rails, inputs and connectors | Anything that needs power |
| Powered | Is the supply right, so the board is safe to program? | First power and idle input current with the MCU blank or in reset; every rail in band; soak; the supply under load (a load sweep on a regulator output) | Buses (CAN, I2C, UART levels and traffic), sensor front ends, current in an operating mode: they depend on firmware |
| Program and function | Does each function work? | Programming, then each interface and sensor, bus idle levels and traffic, and the current in each operating mode | Rail checks that should have stopped the board earlier |
| Characterisation | How well does it work? | Noise, accuracy, drift, captures reduced to statistics | Pass/fail screening already covered above |

Rule of thumb: if the reading depends on what the firmware does, it is a function test. Powered testing ends with a board you are willing to program. Write that as the purpose and exit line of the step's first section, so the operator sees it at the bench.

## 2. Front matter

```markdown
---
plan_format: 1
project: DEMO-1
type: test-plan
status: approved
---
```

`plan_format: 1` is required. Other keys are ignored by Polybench and kept for your notes tool.

## 3. Tier sections and groups

The plan has up to four tier sections, with exactly these headings (an em dash; a hyphen also works):

| Heading | Generates | Order at the bench |
|---|---|---|
| `## Tier 1 — Unpowered` | Unpowered testing, every board | one test across all boards (row mode) |
| `## Tier 1 — Powered` | Powered testing, every board | board by board |
| `## Tier 2 — Unpowered (deeper review)` | Unpowered, only boards sent to Tier 2 | |
| `## Tier 2 — Powered (deeper review)` | Powered, only boards sent to Tier 2 | |

Everything else in the document (purpose, equipment, calculations, open items) is prose and is not read, including tables.

Under a tier section, each **group** is a `###` heading that ends with the designators it covers, at most one instruction paragraph, and exactly one table:

```markdown
## Tier 1 — Unpowered

### Input and rails (J1, D1, C7, U1, U2, U3)

Meter on resistance. Red lead on Probe +, black on Probe −. **Board unpowered.**

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| U01 | VIN to GND resistance | TP1 | TP2 | VIN, GND | J1, D1 | number | kOhm | 10 | — | hard | DEMOCAN DS-001 p.5 Table 2 | below 1: VIN looks shorted, do not power this board | — | — |
| U02 | 5V to GND resistance | TP3 | TP2 | 5V, GND | U2 | number | kOhm | 2 | — | guess | closes on: first powered board current | — | x0.5..x2 | — |
| U03 | 3V3 to GND resistance | TP4 | TP2 | 3V3, GND | U1 | number | kOhm | 1 | — | tbr | closes on: load figure in DEMOMCU datasheet, current revision | — | ±25% | — |

- U01: Wait two seconds for the input capacitors to charge before reading.
```

- The designators in the heading must be in the BOM; they are what coverage counts as covered. Write `(—)` for none.
- The instruction paragraph is shown above the tests. Per-test instructions go in a bullet list after the table: `- U01: ...`.
- Empty cells are written `—`.

## 4. The fifteen columns, in short

| Column | DEMO-1 example | Rule |
|---|---|---|
| ID | `U01` | Unique in the plan; upper case; never reused for a different measurement |
| Test | `VIN to GND resistance` | For a choice test end with `(options: a / b / c)` |
| Probe +, Probe − | `TP1`, `TP2` | Red and black lead. A pad that is not in the BOM is a warning |
| Nets | `VIN, GND` | Selected in the ECAD program with the test |
| Highlight | `J1, D1` or `J1.1` | Parts or `PART.PIN`; must be in the BOM |
| Kind | `number` | `number`, `yn`, `choice` or `text` |
| Unit | `kOhm` | From a fixed vocabulary; required for `number` |
| Min, Max | `10`, `—` | Inclusive; a hard or guess number test needs at least one |
| Class | `hard` | `hard`, `guess`, `tbr` or `record` |
| Source | `DEMOCAN DS-001 p.5 Table 2` | Hard: `DOC p.N Table X`, `DOC p.N Section X` or `calc Cn`. Guess and tbr: `closes on: ...` |
| Stop if | `below 1: VIN looks shorted, ...` | `below X:`, `above X:`, `outside A..B:` or `on No:`, always with the text |
| Outlier | `x0.5..x2`, `±25%`, `±25% (min 2 mA)` | Band against the batch median, read by the decision gate |
| Inputs | `keyboard, voice, action` | `—` means keyboard and voice |

A `yn` test passes on Yes: phrase it so Yes is the good answer. The full rules and the unit vocabulary are in the [reference](../reference/plan-format.md).

## 5. Instrument actions

The plan cannot hold plug-in parameters, so the protocol defines each action and the plan places it in a group with an `Actions:` line:

```markdown
## Tier 1 — Powered

### First power (U1, U2, U3, C1, C2, C3, C4, C5, C6)

Actions: read_rails
Set the supply to 12 V, current limit 150 mA. Switch on, read, switch off.

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P01 | Supply current at 12 V | — | — | VIN | — | number | mA | — | 40 | hard | calc C1 | above 100: over-current, switch off now | ±25% (min 2 mA) | keyboard, voice, action |
| P02 | 5V rail | TP3 | TP2 | 5V | U2 | number | V | 4.85 | 5.15 | hard | DEMOBUCK DS p.3 Table 1 | outside 4.5..5.5: rail out of range, switch off | — | keyboard, voice, action |
```

In the protocol (any fragment), the action with that id:

```yaml
actions:
  - id: read_rails
    title: Read rails and current
    plugin: demo.measure            # on a real bench, for example n6705b.first_power
    params:
      values: {P01: [22, 0.15, mA], P02: [5.0, 0.01, V], P03: [3.3, 0.01, V], P04: [14, 0.2, mA]}
    proposes: [P01, P02, P03, P04]
```

The action's `params` say which test receives which value; `proposes` lists the same IDs so the loader can check them. See [Plug-ins and instruments](plugins.md).

## 6. Tier 2 and the decision gate

Tier 2 groups are written like Tier 1 groups. They apply only to the boards the decision gate (or you) sends there:

```markdown
## Tier 2 — Unpowered (deeper review)

### Rail fault finding (C1, C2, C3, C4, C5, C6)

Only for boards the decision gate sends here. Lift one end of each decoupling capacitor in turn until the low reading clears.

| ID | Test | ... |
|---|---|---|
| U10 | 5V to GND with the capacitors lifted | TP3 | TP2 | 5V, GND | C1, C2, C3 | number | kOhm | — | — | record | — | — | — | — |
```

### The decision gate

A `## Decision gate` section holds prose and one fenced block with the language `gate`:

````markdown
## Decision gate

```gate
after: Tier 1 Unpowered
to: Tier 2 Unpowered
any out of band
any stop
outlier from 5 boards
hold power
halt when more than 50 % of boards are out of band on one test
```
````

| Line | Meaning |
|---|---|
| `after:` | Which Tier 1 readings the gate looks at (required) |
| `to:` | Which Tier 2 sections a board is sent to |
| `any out of band` | A hard FAIL, a guess review or a yes/no answered No sends the board |
| `any stop` | A tripped stop rule sends the board |
| `outlier from N boards` | Use the Outlier column once N boards have a reading |
| `hold power` | The power interlock: the board cannot be powered until Tier 2 is settled and Tier 1 repeated |
| `halt when more than P % ...` | Tell the operator to stop the batch when one test is out of band on many boards |

The app proposes; the operator accepts or rejects each board. See [Unpowered testing: the decision gate](../steps/unpowered-testing.md#the-decision-gate).

## 7. Point the protocol at the plan

```yaml
# demo-docs.pcbv.yaml
schema: pcbv.protocol/2
design: {name: DEMO-1, revision: "A", plan_revision: 1}
batch: {default_boards: 10, label_prefix: S}
test_plan: {file: DEMO-1_Test_Plan.md, generate: true}
include:
  - ../../../src/polybench/examples/demo/assembly.yaml
  - ../../../src/polybench/examples/demo/visual.yaml
  - ../../../src/polybench/examples/demo/unpowered.yaml      # its sections are replaced; its actions stay available
  - ../../../src/polybench/examples/demo/powered.yaml        # defines the read_rails action
  - ../../../src/polybench/examples/demo/function.yaml
  - ../../../src/polybench/examples/demo/characterisation.yaml
  - aoi/aoi.yaml
```

At start Polybench parses and lints the plan and replaces the sections of the steps the plan defines. A plan error stops the start and lists the plan's line numbers.

## 8. Lint until clean

```bash
polybench plan lint DEMO-1_Test_Plan.md
```

```text
# protocol: .../demo-docs.pcbv.yaml
.../DEMO-1_Test_Plan.md:28: warning: U01: Probe + 'TP1' is not in the BOM
...
# DEMO-1_Test_Plan.md: 12 tests in 4 groups, 0 errors, 15 warnings, hash 8da1918ad09bcdbd
```

The command finds the protocol that points at the plan (next to it, or in a `polybench/` folder beside it) and uses its BOM and actions for the checks; `--protocol` names it explicitly. DEMO-1's warnings are expected: its test points are pads, not BOM parts. `polybench plan import` writes the generated protocol steps, so you can read exactly what the bench will run. Then lint the whole protocol:

```bash
polybench lint demo-docs.pcbv.yaml
```

## 9. The steps the plan does not hold

Assembly, Visual, Program and function, and Characterisation stay in the protocol. In format 1, `## Function`, `## Characterisation` and `## Visual` sections in a plan are reserved: they are linted but nothing is generated from them. The DEMO-1 fragments show each:

=== "BOM (assembly.yaml)"

    ```yaml
    steps:
      - id: assembly
        title: Assembly aid
    bom:
      - {line: 1, order: 3, designators: [U1], mpn: "DEMOMCU-32", package: TQFP-32,
         polarity: "pin 1 dot to the chamfered corner", dnf: false,
         barcode: ["595-DEMOMCU32", "DEMOMCU-32"], side: top, note: "Place first, tack two corner pins"}
      - {line: 8, order: 8, designators: [R1, R2], mpn: "RC0402FR-07120RL", value: "120 Ohm",
         package: "0402", dnf: true, note: "CAN termination: NOT fitted on this board"}
    ```

=== "Visual (visual.yaml)"

    ```yaml
    steps:
      - id: visual
        title: Visual
        sections:
          - id: solder
            title: Solder and placement
            instructions: "Inspect under the lamp. **N** is a fail; write what you see in a note."
            tests:
              - {id: V01, title: No solder bridges on U1, kind: yn, highlight: [U1]}
              - {id: V03, title: D1 and C7 polarity, kind: yn, highlight: [D1, C7]}
    ```

=== "Function (function.yaml)"

    ```yaml
    steps:
      - id: function
        title: Program and function
        gate: {requires: [powered]}
        sections:
          - id: program
            title: Program
            actions:
              - {id: flash, title: "Flash bring-up image", plugin: demo.flash,
                 params: {device_id_test: F01, ok_test: F02}, proposes: [F01, F02]}
            tests:
              - {id: F01, title: Device ID, kind: hex,
                 limit: {class: hard, equals: "0x10810A05", source: {type: datasheet, doc: "DEMOMCU DS", section: "7.3"}}}
    ```

=== "Characterisation (characterisation.yaml)"

    ```yaml
    steps:
      - id: characterisation
        title: Characterisation
        applies_to: [S01]
        gate: {requires: [function]}
        sections:
          - id: noise
            title: Front-end noise
            actions:
              - id: capture
                title: Capture noise at 2 mV/V
                plugin: demo.capture
                params:
                  prompt: "Fit the 350 Ohm bridge simulator on J1 and set it to 2 mV/V, then press Continue."
                  captures: 4
                  tests: {mean: C01, std: C02, pp: C03}
                proposes: [C01, C02, C03]
    ```

Every key is in the [Protocol file reference](../reference/protocol.md).

## 10. Rehearse with --simulate

```bash
polybench --protocol demo-docs.pcbv.yaml --simulate --batch rehearsal --port 8751
```

Walk every step once with simulated actions. It shows missing probe points, unclear instructions and tests in the wrong order before boards are on the bench.

## Checklist before the bench

- [ ] Every hard limit cites a page and table, or a calculation.
- [ ] Every guess and tbr says what closes it.
- [ ] Every powered test that could damage the board has a stop rule.
- [ ] The powered step holds only supply and rail tests; bus, sensor and per-mode current tests are in Program and function (section 1a).
- [ ] The gate says what sends a board to Tier 2, and whether power is held.
- [ ] `polybench plan lint` and `polybench lint` report 0 errors.
- [ ] Step 4 coverage lists no part you meant to test.
- [ ] A simulated rehearsal walked every step.
- [ ] `polybench doctor --protocol ...` is green for the instruments you will use.
