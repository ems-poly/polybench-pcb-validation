# Test plan format <span class="badge preview">Preview</span>

Reference for `plan_format: 1`, the markdown test plan from which Polybench generates the Unpowered and Powered steps. For a walk-through see [Writing a test plan](../guides/writing-a-test-plan.md).

**must** means the lint reports an error and nothing is generated; **should** means a warning.

## What is read

| Part of the document | Treatment |
|---|---|
| Front matter | `plan_format` is read and must be 1. Other keys are ignored. |
| `%% ... %%` comments | Removed before parsing; line numbers are kept. |
| `## Tier N — ...` sections | Read. |
| `## Decision gate` | Read: its fenced `gate` blocks. |
| `## Function`, `## Characterisation`, `## Visual` | Reserved: linted, nothing generated in format 1. |
| Fenced code blocks | Skipped, except `gate` blocks. |
| Any other heading, paragraph, list or table | Ignored. |

A heading that starts with `Tier` and a number but does not match the four forms is an error, so a typo cannot hide a section.

## Tier sections

| Heading | Step | Tier | Bench order |
|---|---|---|---|
| `## Tier 1 — Unpowered` | `unpowered` | 1 | one test across all boards (row mode) |
| `## Tier 1 — Powered` | `powered` | 1 | board by board |
| `## Tier 2 — Unpowered (deeper review)` | `unpowered` | 2 | as the step |
| `## Tier 2 — Powered (deeper review)` | `powered` | 2 | as the step |

Each at most once. Tier 2 sections are conditional: a board enters them only when the gate or the operator sends it, and for every other board they are n/a.

## Groups

```markdown
### <Group title> (<designators covered>)

Actions: action_id, other_id
<one instruction paragraph, optional>

| ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ... |

- U03: <per-test instruction>
```

- The heading must end with the covered designators in parentheses, `(—)` for none; each must be in the BOM.
- At most one instruction paragraph; exactly one table.
- `Actions:` names protocol-defined actions to show as Run buttons in this group.
- The section id is `t<tier>_<slug of the title>`; two groups with the same id in one step are an error.

## Columns

The header is exactly these fifteen columns, in this order. Every row has fifteen cells. Empty cells are `—`.

| Column | Rule |
|---|---|
| **ID** | Upper-case letters, digits, underscore, starting with a letter, optional lower-case suffix (`U03`, `P10a`). Unique in the plan, case-insensitively. One digit width per prefix. Not a reserved word (`pass`, `fail`, `ok`, `note`, `next`, `skip`, `accept`, `reject`, `undo`, `back`, `repeat`, `all`, `row`, `board` and similar) and not a board name. |
| **Test** | Required. A choice test ends with `(options: a / b / c)`. |
| **Probe +**, **Probe −** | A designator or pad name, or `—`. Not in the BOM: warning. |
| **Nets** | Comma-separated net names. |
| **Highlight** | Comma-separated `DES` or `DES.PIN`; every designator must be in the BOM. |
| **Kind** | `number`, `yn`, `choice` or `text`. |
| **Unit** | Required for `number`, from the vocabulary below. |
| **Min**, **Max** | Numbers, inclusive, `number` tests only. A hard or guess number test needs at least one. |
| **Class** | `hard`, `guess`, `tbr` or `record`. |
| **Source** | See below. |
| **Stop if** | See below. |
| **Outlier** | See below. `number` tests only. |
| **Inputs** | From `keyboard`, `voice`, `action`. `—` means keyboard and voice. |

**Units:** `Ohm kOhm MOhm mOhm V mV uV kV A mA uA Hz kHz MHz s ms us % degC ppm mV/V mm count dB`. `Ω`, `kΩ`, `MΩ`, `mΩ`, `µV`, `µA`, `µs` and `°C` are normalised. Any other unit is an error.

**yn tests** pass on Yes; a hard or guess yn test fails or flags on No. **choice** tests are `record` or `tbr`; **text** tests are `record`.

## Class and Source

| Class | Outside the band | Source |
|---|---|---|
| `hard` | FAIL | `DOC p.N Table X`, `DOC p.N Section X`, or `calc Cn` (`calc C4, C5`). Required. |
| `guess` | review, never FAIL | `closes on: <what sets the real value>`. Required. |
| `tbr` | no verdict; listed as open | `closes on: ...`. Required. |
| `record` | no verdict | `—` |

A hard test with a `closes on:` source, or a guess, estimate or assumption word, is an error.

## Stop if

| Written | Trips when |
|---|---|
| `below 0.12: text` | the reading is below 0.12 |
| `above 100: text` | the reading is above 100 |
| `outside 0.2..1.2: text` | the reading is outside the range |
| `on No: text` | a yn test is answered No |
| `—` | never |

Units are the row's unit. The text after the colon is what the operator reads. Choice and text tests take no stop rule.

## Outlier

| Written | Outlier when the reading is |
|---|---|
| `x0.5..x2` | below 0.5 times or above 2 times the batch median |
| `±25%` | more than 25 % from the batch median |
| `±25% (min 0.5 mA)` | the same, and at least 0.5 mA away |
| `—` | never |

The median is over the boards tested so far in the current attempt. The minimum board count is a gate rule (`outlier from N boards`).

## Decision gate

One or more fenced blocks with the language `gate` under `## Decision gate`:

| Line | Meaning |
|---|---|
| `after: Tier N [Unpowered\|Powered]` | Required. The readings the gate looks at. |
| `to: Tier M [Unpowered\|Powered]` | The Tier 2 sections a board is sent to (default: both). |
| `any out of band` | A hard FAIL, a guess review or a yn No sends the board. |
| `any stop` | A tripped stop rule sends the board. |
| `outlier from N boards` | Use the Outlier column once N boards have a reading. |
| `hold power` | Power interlock for the powered step. |
| `halt when more than P % of boards are out of band on one test` | Shows a stop-the-batch message. |

An unknown line, a gate without `after`, a tier the plan lacks, or `outlier` with no Outlier entries is an error.

## Generation

`polybench plan import <plan.md> [--protocol P] [--out fragment.yaml]` writes the generated steps; nothing is written when the plan has errors. The fragment starts with `# GENERATED from <plan> @ <hash>, do not edit.` With `test_plan: {file, generate: true}` in the protocol, the app does the same at start and on **Regenerate tests**. See [Step 4](../steps/test-plan.md).

## Lint messages

Every finding is `<plan file>:<line>: error|warning: <text>`. Errors cover the front matter, headings, groups, columns and cells, IDs, kinds and units, limits and sources, stop rules, outlier bands, inputs, BOM designators, actions and gate rules as listed above. Warnings cover a Tier 2 heading without `(deeper review)`, `-` or a blank instead of `—`, a probe pad not in the BOM, a record test with a source or limits, a unit on a non-number test, an `action` input without an `Actions:` line, text after a table, and a plan linted without a BOM.
