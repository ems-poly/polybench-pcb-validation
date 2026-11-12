# Test plan format (plan_format 1)

Status: format 1, implemented by `src/polybench/planfmt.py` (16 Oct 2026). HOST-ONLY: the parser, the lint, the generator and the app behaviour are covered by unit tests and a simulated end-to-end run; nothing here has been run against a board. Living document, edited in place (revision history at the end).

The **test plan is the master**. It is a markdown document a person reviews in Obsidian and an agent can write or import. Polybench reads it and **generates the tests, the sections, the limits, the stop rules, the Tier 2 rules and the decision gate from it**. Coverage is measured against the BOM from the same tables. The protocol YAML keeps what a plan cannot hold: the batch, the BOM, the capabilities, the instrument actions and everything outside the tier steps.

Related documents: the [plug-in reference](PLUGINS.md) and the user-manual pages on the [plan format](../site/docs/reference/plan-format.md) and the [protocol file](../site/docs/reference/protocol.md). The inconsistencies of an earlier, simpler plan format are answered in the section [What this format avoids](#what-this-format-avoids).

Words used: **must** = the lint reports an error and nothing is generated; **should** = a warning; **ignored** = the parser does not read it.

## 1. What is read, what is ignored

| Part of the markdown | Treatment |
|---|---|
| Front matter (first `---` block) | Read for `plan_format`. Every other key is a vault property and is ignored. |
| `%% ... %%` comments | Removed before parsing; line numbers are kept. |
| Fenced code blocks | Skipped, except the `gate` block inside `## Decision gate`. |
| `## Tier N — ...` sections | **Read** (section 3). |
| `## Decision gate` | **Read** (section 8). |
| `## Function`, `## Characterisation`, `## Visual` | **Reserved** (section 9): parsed and linted like a tier section, not generated yet. |
| Any other heading, paragraph, list or table | **Ignored.** A plan has prose sections (how it works, equipment, symptom map, not covered, open items, revision history); a table in one of them is never read as tests. |

A heading that starts with `Tier` and a number but does not match section 3 is an error, so a typo cannot hide a section.

## 2. Front matter

```yaml
---
plan_format: 1
living: true
project: demo
type: plan
status: draft
---
```

`plan_format` must be present and must be 1. A parser that reads format 1 refuses another number. A change to this document that alters what the parser reads raises the number; wording changes do not.

## 3. Tier sections, groups and tables

### 3.1 Tier sections

Exactly these four headings, each at most once:

| Heading | Protocol step | Tier | Row mode |
|---|---|---|---|
| `## Tier 1 — Unpowered` | `unpowered` | 1 | one test across all boards, then the next (`advance: row`) |
| `## Tier 1 — Powered` | `powered` | 1 | board by board (`advance: board`) |
| `## Tier 2 — Unpowered (deeper review)` | `unpowered` | 2 | as the step |
| `## Tier 2 — Powered (deeper review)` | `powered` | 2 | as the step |

The dash is an em dash. A hyphen or en dash is accepted. `(deeper review)` is expected on Tier 2 headings and is a warning when missing.

**Step scope.** `## Tier N — Powered` holds supply and rail tests only (first power, idle current with the MCU blank or in reset, rails, soak, supply under load); tests that depend on firmware (bus levels, sensor front ends, per-mode current) go in the function step. Guide: site/docs/guides/writing-a-test-plan.md section 1a.

**Tier 1** sections apply to every board. **Tier 2** sections are *conditional*: the generated section carries `tier: 2`, `conditional: true` and `applies_to: operator`. A Tier 2 section applies to a board only for the boards the decision gate (or the operator) sent there, for the current attempt of the step. For every other board it is **n/a**: it does not count towards the step's progress, towards the `powered` and `function` gates, or towards the report's open items, and an entry for it is refused (HTTP 409). Sending a board to Tier 2 is a journal event (`tier2_assign`) with the reason; opening a retest of the step starts a new attempt and the board is n/a for Tier 2 again until it is sent again.

### 3.2 Groups

Under a tier section each group is

```markdown
### <Group title> (<designators covered, comma separated>)

Actions: first_power
<one instruction paragraph, optional>

| ID | Test | ... |
|---|---|---|
| ... |

- U03: <per-test instruction, optional>
```

- The heading must end with a parenthesis holding the designators the group covers: `(IC4, L1, C1)`. Write `(—)` for none. Each must be in the BOM (error otherwise). These are what coverage counts as covered by the group.
- **At most one instruction paragraph** (blank-line separated). It becomes the section's `instructions`. More than one is an error: detail goes in the per-test list.
- **Exactly one table.** A second table in the group is an error; a group without a table is an error.
- A table directly under a tier heading, outside any group, is an error.
- The group title becomes the section title; the section id is `t<tier>_<slug of the title>` (lower case, non-alphanumerics to `_`, at most 40 characters). Two groups of one step that make the same id are an error. Ids are stable while the title is stable; protocol fragments and the journal refer to a test by its test ID, never by section id.

### 3.3 Actions line

A line `Actions: id1, id2` at the start of a paragraph (it is removed from the instruction text) names the instrument actions the group offers as Run buttons. The plan cannot hold plugin parameters, so the **protocol defines the actions** (their title, plugin, params and `proposes`) in an ordinary section of a fragment, and the plan chooses where each appears. An id the protocol does not define is an error. A hand-written action stays available as this library even though its section is replaced. A test whose Inputs include `action` in a group with no `Actions:` line is a warning.

### 3.4 Per-test instructions

A bullet list directly after the table, one item per test: `- U03: Red lead on the anode.` Continuation lines are indented. An id that is not in this group's table is an error. The text becomes the test's `instructions`. Other text after the table is ignored with a warning.

### 3.5 The table

The header is exactly these fifteen columns in this order (case-insensitive; `−` and `–` are accepted for the minus in `Probe −`):

`ID | Test | Probe + | Probe − | Nets | Highlight | Kind | Unit | Min | Max | Class | Source | Stop if | Outlier | Inputs`

An unknown column, a missing column, a duplicate column, or a different order is an error that names the column. Every row must have fifteen cells; a row with another count is an error (an unescaped `|` inside a cell is the usual cause: write `\|`). **Empty cells are written `—`.** A blank cell, `-` or `–` is read as empty with a warning.

| Column | Rule | Generated field |
|---|---|---|
| **ID** | Upper-case letters, digits, underscore, starting with a letter; an optional lower-case suffix letter (`U03`, `P10a`). **Unique across the plan, case-insensitively.** One digit width per letter prefix (`U03`, never `U3` beside it). Not a reserved word (`pass`, `fail`, `ok`, `okay`, `note`, `text`, `defect`, `next`, `skip`, `accept`, `reject`, `undo`, `back`, `repeat`, `all`, `row`, `board`) and not a board name (`B1`). | `id` |
| **Test** | Required text. For a `choice` test it ends with `(options: first / second / third)`, options separated by ` / ` (options may contain parentheses). | `title`, `options` |
| **Probe +**, **Probe −** | A designator or pad name (`TP1`, `GND1`, `C12`, `EXC+`), `—` when no meter lead applies. Plus is the red lead, minus the black. Not being in the BOM is a warning. | `probe: [{point, pole}]` |
| **Nets** | Comma-separated net names, or `—`. Used for the net highlight and the probe card. | `nets` |
| **Highlight** | Comma-separated `DES` or `DES.PIN` (`IC1.12`, `TP1`), or `—`. Every designator must be in the BOM (error otherwise). | `highlight` (parts), `highlight_pins` |
| **Kind** | `number`, `yn`, `choice` or `text`. | `kind` |
| **Unit** | Required for `number`; from the vocabulary below; `—` otherwise. | `unit` |
| **Min**, **Max** | Numbers (`1.0`, `-0.5`, `3e-3`), or `—`. For `number` only. Both given: Min must not exceed Max. A `hard` or `guess` number test needs at least one. A `record` test ignores them (warning). `yn`, `choice` and `text` take `—` in both. | `limit.min`, `limit.max` |
| **Class** | `hard`, `guess`, `tbr` or `record` (section 4). | `limit.class` |
| **Source** | Section 4. | `limit.source`, `limit.closes_on` |
| **Stop if** | Section 5. | `stop_if` |
| **Outlier** | Section 6. | `outlier` |
| **Inputs** | Comma-separated from `keyboard`, `voice`, `action`. `—` means `keyboard, voice`. | `inputs` |

**Unit vocabulary:** `Ohm kOhm MOhm mOhm V mV uV kV A mA uA Hz kHz MHz s ms us % degC ppm mV/V mm count dB`. The spellings `Ω`, `kΩ`, `MΩ`, `mΩ`, `µV`, `µA`, `µs` and `°C` are normalised. Any other unit is an error: a free unit would turn off the checks. The vocabulary grows by a new revision of this document, not by a plan.

**yn tests.** The test passes on **Yes**. A `yn` test of class `hard` or `guess` generates `limit.equals: yes`: No is outside the limit (fail for `hard`, review for `guess`). Min and Max must be `—`. A `record` yn test records the answer with no verdict. There is no way to write "pass on No": phrase the test so Yes is the good answer.

**choice tests** record the option chosen, class `record` or `tbr`. **text tests** record what is typed, class `record`.

## 4. Class and Source

| Class | Verdict | Source (required form) |
|---|---|---|
| `hard` | Out of band is **fail**. | `DOC p.N Table X` or `DOC p.N Section X` (`DEMOMCU DS12345 p.10 Table 4`), or `calc Cn` (`calc C4`, `calc C4, C5`; `[[calc]] C4` is read the same). **Required.** |
| `guess` | Out of band is **review** (amber), never fail. The value is recorded. | `closes on: <what measurement or decision sets the real value>`. **Required.** |
| `tbr` | No verdict; listed as open in the report. | `closes on: ...`. **Required.** |
| `record` | No verdict. | `—`. |

Errors: a `hard` test with a missing source; a `hard` test whose source is `closes on:` or a guess/estimate/assumption word (a hard limit with a guess source); a `guess` or `tbr` test without `closes on:`; a source that fits none of the forms.

## 5. Stop if

One rule per row, always with the text the operator reads:

| Written | Meaning | Generated |
|---|---|---|
| `below 0.12: text` | the reading is below 0.12 | `stop_if: {below: 0.12, text}` |
| `above 100: text` | the reading is above 100 | `{above: 100, text}` |
| `outside 0.2..1.2: text` | the reading is outside the range (A must not exceed B) | `{outside: [0.2, 1.2], text}` |
| `on No: text` | a yes/no test is answered No | `{equals: no, text}` |
| `—` | no stop rule | none |

The units are the row's Unit. A stop rule trips when the value is recorded; the board is stopped until the operator acknowledges it with a reason. `on No` is for `yn` tests only; `choice` and `text` tests take no stop rule. Any other text, a missing `: text`, or a malformed number is an error ("malformed Stop if") with the line number.

## 6. Outlier

The per-test band the decision gate reads (section 8). Only `number` tests.

| Written | Meaning |
|---|---|
| `x0.5..x2` | outlier if the reading is below 0.5 times or above 2 times the batch median (needs 0 < lo < 1 < hi) |
| `±25%` | outlier if the reading differs from the batch median by more than 25 % |
| `±25% (min 0.5 mA)` | the same, and by at least 0.5 mA (the unit is the row's) |
| `—` | no outlier check |

The batch median is taken over the readings recorded on the boards tested so far, in the current attempt of the step. The reading 99999 (the plan types it for OL) never takes part in a median and is never an outlier. The minimum number of boards is a gate rule (`outlier from N boards`); below it the check is skipped and only the limit applies. A `hard` limit normally has no band: the limit is already narrow.

## 7. Generated protocol

`polybench plan import <plan.md> [--protocol P] [--out fragment.yaml]` writes the generated steps (to standard output without `--out`; **nothing is written when the plan has errors**). The fragment starts `# GENERATED from <plan> @ <hash>, do not edit.` where the hash is the first 16 hex digits of the SHA-256 of the plan file. `--protocol` supplies the BOM and the action library; without it the designator and action checks are skipped with a warning (the command finds a `*.pcbv.yaml` next to the plan or in its `polybench/` folder whose `test_plan` points at the plan). `polybench plan lint <plan.md>` lints only.

Generated per step: `id`, `kind`, `advance` (`row` for unpowered, `board` for powered), `plan_generated: true`, and `sections`, each with `id`, `title`, `designators`, `tier`, `instructions`, `actions`, `tests`, and for Tier 2 `applies_to: operator` and `conditional: true`. Each test has `id`, `title`, `kind`, `unit`, `options`, `limit {class, min, max, source, closes_on, equals}`, `probe`, `nets`, `highlight`, `highlight_pins`, `stop_if`, `outlier`, `inputs`, `instructions`, `tier`. The decision gate becomes top-level `tier_gates`.

**Protocol key.**

```yaml
test_plan: {file: ../DEMO-1_Test_Plan.md, generate: true}
```

`file` is relative to the top protocol file. `test_plan: <path>` alone (a string) only displays the plan in design step 4. With `generate: true` the app loads the tests of the steps the plan defines **from the plan at start**: it parses the plan, lints it, and replaces the sections of those steps. Steps the plan does not define (assembly, visual, function, characterisation) come from the protocol as before. Hand-written tests inside a generated step are replaced (one warning per step); their actions stay as the action library. A plan with a lint error stops the start (the error list carries the plan's line numbers); the **Regenerate tests** button in step 4 and `POST /api/cmd/plan_reload` read the plan and the protocol again without a restart, and keep the running tests when the new plan has errors.

**Journal and revisions.** At start (and on each regenerate) the app journals `plan_loaded` with the plan hash, the test count and the list of test ids, unless the last `plan_loaded` has the same hash. The protocol hash does not include the plan file, so a plan edit is not shown as "protocol changed"; it is a new `plan_loaded`. **Results are kept by test ID across plan revisions** (the journal keys every value by board, test ID and attempt). A test the plan no longer has is **retired**: it is listed under "Retired" in its step and in step 4 with its recorded results, and its values stay in the journal and the report. Re-adding the ID brings the results back. For that reason **a test ID is never reused for a different measurement**; retire it and take the next free number.

**Coverage (design step 4, under the rendered plan).** Tests generated per tier and step and per section; the designators covered (group headings, Highlight, Probe cells) against the BOM with the uncovered parts listed (DNF parts and test pads included, marked); the lint count with each finding and its plan line; the decision gates; the retired ids; the last plan loads.

## 8. Decision gate

`## Decision gate` holds prose and one or more fenced blocks with the language `gate`. Each block is one gate. Lines (blank lines and `#` comments ignored; a leading `-` is allowed):

```gate
after: Tier 1 Unpowered
to: Tier 2 Unpowered
any out of band
any stop
outlier from 5 boards
hold power
halt when more than 50 % of boards are out of band on one test
```

| Line | Meaning |
|---|---|
| `after: Tier N [Unpowered\|Powered]` | **Required.** The readings the gate looks at: the non-conditional (Tier N) tests of that step, or of both steps when no mode is given. |
| `to: Tier M [Unpowered\|Powered]` | The Tier 2 sections a board is sent to (default `Tier 2`: both steps). |
| `any out of band` | A recorded value outside its limit: a `hard` fail, a `guess` review, or a yes/no answered No. Guess limits count here although the app does not fail them. |
| `any stop` | A stop rule tripped in the current attempt. |
| `outlier from N boards` | Use the Outlier column: a reading outside its band against the batch median, once N boards have a reading. |
| `hold power` | The power interlock (below) applies to the powered step. |
| `halt when more than P % of boards are out of band on one test` | Shows "stop the batch and tell the project owner": the same test out of band on more than P % of the boards tested (at least three). |

A rule line the parser does not know is an error with its line number, as is a gate without `after`, a gate that names a tier the plan does not have, and an `outlier` rule when no test has an Outlier entry. The value X of an outlier band is whatever the plan states in the Outlier column; the format fixes no default.

**What the app does with it.** For each board the gate evaluates only when every test in its scope is settled (otherwise "waiting"). Out of band or stop: **Tier 2**, with the reasons. Outliers only: for a gate over the unpowered step, "outlier, continue" (the board is powered; the gate after the powered readings sends it); for a gate over both steps, Tier 2. Otherwise Tier 2 is skipped. The gate shows a table with Accept and Reject per proposal; **Accept** journals `tier2_assign` with the reasons and opens the Tier 2 sections of the target steps for that board; **Reject** (reason required) journals `gate_reject`. The app proposes; the operator decides.

**Power interlock** (`hold power`). A board with an out-of-band or stopped Tier 1 unpowered reading cannot be powered (the `powered` step is gated for it, overridable with a reason like any gate) until (1) its Tier 2 sections of that attempt are settled and (2) Tier 1 unpowered has been repeated (a retest of the step opened after the rework) and is settled again. Without a `hold power` rule there is no interlock.

## 9. Reserved section types

`## Function`, `## Characterisation` and `## Visual` are reserved for the steps `function`, `characterisation` and `visual`. In format 1 they follow the group and table rules above and are linted, but **nothing is generated from them** and a warning says so; those steps stay in the protocol. A later format number will define their columns (a function test needs a map to a firmware item; a characterisation test needs a capture definition) and generate them.

## 10. Lint catalogue

Every finding is `<plan file>:<line>: error|warning: <text>`.

| Severity | Finding |
|---|---|
| error | front matter without `plan_format: 1`; unsupported format number |
| error | malformed tier heading; duplicate tier section; table outside a group; group heading without `(designators)`; group designator not in the BOM; two instruction paragraphs; two tables; no table |
| error | unknown, missing, duplicate or misordered column; wrong cell count |
| error | ID not well formed, reserved, duplicate (also by case), mixed digit width |
| error | Kind or Class not in the list; number test without Unit or with a unit outside the vocabulary; Min or Max not a number; Min above Max; `hard` or `guess` number test without Min and Max; yn, choice or text with Min or Max; choice without `(options: ...)`; `hard` or `guess` on text or choice |
| error | **missing source** on `hard` or `guess`; **hard with a guess source**; `guess` or `tbr` without `closes on:`; unrecognised source |
| error | **malformed Stop if**; `on No` on a non-yn test; stop rule on a text or choice test |
| error | malformed Outlier; Outlier on a non-number test |
| error | input not in `keyboard, voice, action` |
| error | **Highlight designator not in the BOM**; Highlight cell not a designator or `DES.PIN` |
| error | `Actions:` id the protocol does not define; per-test instruction for an ID not in the group |
| error | gate: no `after`; unknown rule; no rules; tier the plan lacks; `outlier` rule with no Outlier entries |
| warning | Tier 2 heading without `(deeper review)`; blank, `-` or `–` for an empty cell; Probe designator not in the BOM; record test with a source or limits; Unit on a non-number test; `action` input without an `Actions:` line; text after the table; reserved section with tables; no BOM given |

## 11. What this format avoids

The earlier format (report generator, `TEST_PROTOCOL_FORMAT.md` v1) listed where its code was lenient or inconsistent. This format answers each.

**Sheet, section 1.12**

| # | Earlier finding | Here |
|---|---|---|
| 1 | `(guess)` in parentheses made a hard limit | Class is its own column; no prose is read for a limit. |
| 2 | `at most`, `within` not read; `below` strict, `at least` inclusive | Min and Max are numbers in their own columns, both inclusive. Stop rules are `below` and `above` with the value strict, stated here. |
| 3 | Only the first limit of a cell counted | One Min/Max pair and one stop rule per row; a second condition is a second row. |
| 4 | `N (DNF)` in a Y/N row not checked | A yn test takes only Yes or No and passes on Yes; explanations go in the Test cell or a note. |
| 5 | Expected per row only; hex never checked | Per row only, stated; no hex kind; every kind has its check or is record. |
| 6 | A missing Notes column silently lost notes | Notes are journal notes (the app's Note button), not a column. |
| 7 | Duplicate IDs only a warning | Error, also by case. |
| 8 | ID case inconsistent between components | Upper case enforced; duplicates compared case-insensitively; one digit width per prefix. |
| 9 | Voice reached boards 1 to 9 only | Not a plan matter: boards are not in the plan. |
| 10 | Unknown units fell back to free text | Unit vocabulary enforced for number tests. |
| 11 | A `B1 note` header was a board column | No board columns exist in a plan. |

**Profile, section 3.5**

| # | Earlier finding | Here |
|---|---|---|
| 1 | Keys the code needs but the loader did not check | Every column and every line is checked; there are no optional keys the parser needs. |
| 2 | A configurable regex that was ignored | Nothing configurable that is not honoured: the plan configures no parser. |
| 3 | One key selected two adapters | Actions are named by id in an `Actions:` line; the protocol defines each. |
| 4 | Keys that were metadata only | Front matter keys other than `plan_format` are named as ignored. |
| 5 | An omitted glob matched every sheet | The plan is bound by the explicit path in `test_plan`, never by a pattern. |

## 12. Open points

| # | Item | What closes it |
|---|---|---|
| 1 | Net highlight in Fusion rests on `Schematic.selectObjects` accepting `Net` and `PinRef` objects (documented as `EcadObject`), not tried on a live board (the live document is read-only for this build). The client tries nets, then pin references, then parts, and reports which. | A live run on a spare board document: record which mode the card reports. |
| 2 | Function, Characterisation and Visual columns. | The next format number. |
| 3 | The outlier minimum-board count and the `halt` percentage are plan values, not format values. | Each plan states them. |

## Revision history

| Revision | Date | Change | Source |
|---|---|---|---|
| r1 | 2026-10-16 | First issue: plan_format 1. Tier sections, groups, the 15-column table with Outlier, Actions line, per-test instruction list, choice options, yn passes on Yes, decision gate block and rules, reserved section types, generation, `test_plan: {file, generate}`, plan_loaded, retired tests, coverage. The test plan is the master of the generated steps. | Initial design |
