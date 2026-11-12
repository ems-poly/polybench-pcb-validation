# Protocol file

The protocol file (`.pcbv.yaml`, schema `pcbv.protocol/2`) describes one board design for Polybench. It is YAML: a top file plus `include:` fragments, merged in order. Steps are merged by `id`. For a worked example see [Writing a test plan](../guides/writing-a-test-plan.md); for the markdown plan that generates the Unpowered and Powered steps see [Test plan format](plan-format.md).

## Top file

```yaml
schema: pcbv.protocol/2
design: {name: DEMO-1, revision: "A", plan_revision: 1}
batch: {default_boards: 10, label_prefix: S}
test_plan: {file: DEMO-1_Test_Plan.md, generate: true}
include: [assembly.yaml, visual.yaml, unpowered.yaml, powered.yaml, function.yaml, characterisation.yaml]
```

| Key | Meaning |
|---|---|
| `schema` | Must be `pcbv.protocol/2` |
| `design.name` | Required. Shown in the app bar and the report |
| `design.revision`, `design.plan_revision` | Shown in the app bar and the report |
| `design.variant` | Optional. A module or population option (for example `N16R8`), shown after the name: `NAME · N16R8`. Keep it out of `revision` |
| `batch.default_boards` | Board count of a new batch (`--boards` overrides) |
| `batch.label_prefix` | Board labels: `S` gives `S01`, `S02`, ... |
| `include` | Fragments, relative to the file that names them; merged in order; a cycle is an error |
| `test_plan` | `{file, generate: true}`: generate the tier steps from the plan. A plain path: show the plan in step 4 only |
| `test_plan_files` | More plan documents to show in step 4 |

## Steps

```yaml
steps:
  - id: unpowered                # setup, assembly, visual, unpowered, powered, function, characterisation, report
    title: Unpowered testing
    gate: {requires: [visual]}   # optional; an override needs a reason
    applies_to: all              # or a list of board labels, e.g. [S01]
    sections: [...]
```

Batch setup and Report are always added. Steps 1 (Project) and 4 (Test plan) are not declared.

## Sections

```yaml
sections:
  - id: rails
    title: Rail resistance
    instructions: "Meter on resistance. **Board unpowered.**"   # markdown, shown above the tests
    actions: [...]               # optional Run buttons
    position: before_placement   # assembly step only, optional (see below)
    tests: [...]
```

**Order of the groups in the Assembly aid.** The left list of the Assembly aid draws the BOM "To do" list between the step's groups. A section with `position: before_placement` (for example `Paste print`) is drawn above it; every other section (`position: after_placement`, or no `position`) is drawn below it, as before, so a protocol without the key looks the same as it did. Groups keep the order of the file within each side. Polybench ships no built-in assembly tips: a protocol that declares no assembly sections shows only the BOM list. `position` on a step other than the assembly step is ignored with a lint warning; any other value is an error.

## Actions

```yaml
actions:
  - id: read_rails
    title: Read rails and current
    plugin: demo.measure         # module.action, see Plug-ins and instruments
    params: {...}                # passed to the plug-in; includes which test gets which value
    proposes: [P01, P02, P03, P04]
    record: auto                 # auto (default): an instrument reading is the result; propose: the operator accepts it
```

`record: auto` (the default) records the action's readings as results when they arrive; `record: propose` leaves them as proposals to accept. See [Results and anomalies](../guides/results-and-anomalies.md). A `prompt` param makes Polybench ask the operator to confirm a set-up step before the action starts. An unknown plug-in is a warning; an action that proposes an unknown test is an error.

## Tests

```yaml
tests:
  - id: U01
    title: VIN to GND resistance
    kind: number                 # number | yn | choice | text | hex
    unit: kOhm
    options: []                  # for choice
    limit:
      class: hard                # hard | guess | tbr | record
      min: 10                    # and/or max; or equals: ...; or in: [...]
      source: {type: datasheet, doc: "DEMOCAN DS-001", page: 5}
      closes_on: "..."           # required for guess and tbr
    probe: [{net: VIN, point: TP1}, {net: GND, point: TP2}]
    highlight: [TP1, TP2]        # designators selected in the ECAD program
    stop_if: {below: 1, text: "VIN looks shorted: do not power this board"}   # below | above | equals | outside: [a, b]
    instructions: "optional per-test note"
    aoi: [BRIDGE, LIFT]          # optional: AOI candidate kinds that map to this test
```

| Limit class | Outside the limit | Needs |
|---|---|---|
| `hard` | FAIL | a source whose type is not a guess, estimate or assumption |
| `guess` | review, never a FAIL | a source and `closes_on` |
| `tbr` | no verdict | `closes_on` (a missing source is a warning) |
| `record` | no verdict | nothing |

A `yn` test without a limit expects yes. A number test with no limit records the value with no verdict.

## BOM

```yaml
bom:
  - {line: 1, order: 3, designators: [U1], mpn: "DEMOMCU-32", value: "", package: TQFP-32,
     polarity: "pin 1 dot to the chamfered corner", dnf: false,
     barcode: ["595-DEMOMCU32", "DEMOMCU-32"], side: top, note: "Place first",
     height_mm: 1.2, body_mm: [7, 7]}
```

| Key | Meaning |
|---|---|
| `line` | Required, unique |
| `designators` | Required, non-empty; a designator may appear on one line only |
| `order` | Protocol placement order (default: `line`) |
| `mpn`, `value`, `package`, `side`, `note` | Shown in the Assembly aid |
| `barcode` | Codes printed on the bag label; the camera scanner matches them |
| `dnf` | Do not fit |
| `polarity` | Free-text orientation hint |
| `polmark` | Structured polarity marking for the polarity card (below) |
| `height_mm`, `body_mm` | Optional hand-assembly hints for "small and low first"; positive numbers |

### Polarity marking (`polmark`)

| Field | Values |
|---|---|
| `symbol` | `diode`, `tvs`, `led`, `led_rgb_ca`, `ic`, `crystal` |
| `family` | Free text |
| `marking` | `band`, `pin1_dot`, `triangle`, `chamfer_underside` |
| `terminal` | `cathode`, `anode`, `pin1`, `pad1` |
| `net` or `pad` | How the polarity pad is found: exactly one pad on that net, or a named pad |
| `nets` | Optional pad-to-net expectations that tie the library pad numbers to the datasheet pin map |
| `legend`, `corner`, `harmless_180`, `caution`, `ref` | Optional; `ref` is the datasheet and page |

Without `polmark`, or when any check fails, the polarity card is amber. See [Assembly aid: polarity card](../steps/assembly-aid.md#polarity-card).

## Capabilities

```yaml
capabilities:
  - {name: programmer, kind: programmer, optional: false}
  - {name: bus_adapter, kind: bus_adapter, optional: false}
  - {name: source, kind: dc_source, optional: true}
  - {name: ecad, kind: ecad, optional: true}
```

Listed in the Controls tab.

## AOI

```yaml
aoi:
  protocol: aoi_protocol.json    # relative to the file that holds this block
  layout: layout.json
  cam_zip: /path/to/board.zip    # optional
```

See [Visual and AOI](../steps/visual-aoi.md#set-up-aoi-for-your-board).

## What lint checks

`polybench lint <file>` prints every issue with file and line, then a summary. Errors stop the server from starting.

Errors include: wrong schema; missing `design.name`; an include that cannot be loaded or repeats; a step, section, action or test without an `id`; duplicate test or action IDs; an unknown test kind; a choice test without options; a limit without a class or with an unknown class; a hard or guess limit without a source; a hard limit with a guess, estimate or assumption source; a guess or tbr limit without `closes_on`; a non-numeric min or max, or min above max; a hard or guess number test with neither min nor max; a malformed `stop_if`; an action that proposes an unknown test; a plug-in name that is not `module.action`; a BOM line without `line`, a duplicate line, empty designators or a designator on two lines; bad `height_mm` or `body_mm`; `applies_to` that is neither `all` nor a list; a section `position` that is not `before_placement` or `after_placement`; and every error of a generated plan.

Warnings include: a plug-in that is not installed here; a tbr limit without a source; a source without a type; a test plan file that is not found; a section `position` on a step other than the assembly step.
