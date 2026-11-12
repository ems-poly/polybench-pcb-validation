# Changelog

The same list is kept in `CHANGELOG.md` in the repository. The format follows Keep a Changelog.

## 0.1.0 (unreleased)

**Built**

- Twelve-step workspace in the browser: steps 1 and 4 to 12.
- Project step: ECAD link check, document selection by fingerprint, BOM cross-check.
- Batch setup, Assembly aid with camera scanner, ECAD highlight, batch placement, list orders, auto-place on scan and polarity card.
- Visual is the AOI review: AOI photos, AOI run, issue review, manual issues, the AOI checklist, Reworked on a confirmed issue, per-board AOI progress. The Yes/No checklist is archived. Live capture is shown as Coming (V2).
- Protocol changed badge opens a dialog with the old and new hash and an Accept new protocol button (journal event `protocol_accepted`).
- Unpowered and Powered testing with limit classes, stop rules, gates, retests and proposals.
- Program and function, Characterisation, Report with readiness labels and errata candidates.
- Append-only journal, operator notes, `polybench doctor`, `polybench selftest`, `polybench lint`, `polybench aoi-derive`.
- Plug-ins: Fusion bridge, N6705B, OpenOCD, PCAN, camera, demo actions.
- Instrument readings are results: recorded when the action delivers them, with an Anomalies strip, verdict chips, Redo, and table results (see [Results and anomalies](guides/results-and-anomalies.md)). Voice and AI values stay proposals.

**Preview**

- Test plan as master (`plan_format: 1`): generated tiers, decision gate, Tier 2, power interlock, coverage, `polybench plan`.
- Voice: push-to-talk and hands-free with local speech to text and a local language model. Bench commands by voice: board select, start (read back, then "confirm"), stop (always works, no confirmation), next test. Guided mode: "guide me" walks a test section one step at a time, runs each step on "ready" and reads the readings back.

**Planned**

- Step 2 Schematic analysis, step 3 BOM and manufacturing, drafting the test plan from the schematic and layout.
- The Altium bridge.

**Changed**

- The distribution is named `polybench-pcb-validation`; the command is `polybench`. There is no `bench` extra: the N6705B actions need the separate, unpublished `polystage-bench` library.
