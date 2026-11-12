# FAQ

**Does Polybench need the internet?**
Only to install it and its optional models. At the bench it runs entirely on your machine. See [Privacy and data](guides/privacy.md).

**Which ECAD programs does it support?**
Autodesk Fusion, through its local MCP server. KiCad 10 is also supported through its IPC API (read-only, tested live on KiCad 10.0.7). The Altium bridge exists as an interface only and is <span class="badge planned">Planned</span>. Without an ECAD program, choose **Continue without ECAD**: everything except highlights and the polarity card works.

**Which instruments does it support?**
Out of the box: a Keysight N6705B power analyzer, OpenOCD with an Atmel-ICE, a PEAK PCAN-USB adapter and USB cameras. Their status is in [Plug-ins and instruments](guides/plugins.md). Anything else is a [plug-in](guides/writing-a-plugin.md). Manual entry from any meter always works.

**Can I use it without any instrument?**
Yes. Type every reading. Instruments and actions are optional.

**What if the laptop crashes in the middle of a batch?**
Start Polybench again with the same `--batch`. Every value you accepted was on disk before the screen showed it.

**Can two people work on one batch at once?**
No. Polybench is for one operator at a time per bench. The server listens on 127.0.0.1 only.

**Why can a guessed limit not fail a board?**
Because a guess is not evidence. A value outside a guess limit is flagged for review and listed with what will close the limit, so a good board is not scrapped on an estimate. See [Concepts: limit classes](concepts.md#limit-classes).

**Why does Enter not record a FAIL?**
So a FAIL is always deliberate. Use Shift+Enter or **Record FAIL**.

**Does AOI pass boards?**
No. AOI lists candidates for you to decide. Finding nothing is never a PASS.

**Can I edit the journal?**
It is a plain JSON-lines file, but Polybench treats it as append-only and so should you. Corrections are new events: a replaced value, a retest, a skip with a reason.

**Is the protocol or the test plan the master?**
For the Unpowered and Powered steps, the test plan when it is attached with `generate: true`. For everything else, the protocol. See [Writing a test plan](guides/writing-a-test-plan.md).

**Does voice send audio anywhere?**
No. Speech to text runs locally; the language model must be on 127.0.0.1. See [Voice](guides/voice.md).

**Which operating systems are supported?**
It is developed and tested on macOS. The core is plain Python; the camera scanner and voice use macOS facilities.

**What licence is Polybench under?**
Not decided yet.
