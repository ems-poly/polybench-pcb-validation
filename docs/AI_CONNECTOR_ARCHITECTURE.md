# AI connector: architecture

Status: r1, 17 Oct 2026. Living document.

Polybench works beside an AI assistant (Claude Code or Codex). The assistant sees what the operator sees, co-authors the test plan as a draft, and helps troubleshoot from readings, AOI photos and ECAD. It never records a result, accepts a FAIL, overrides a gate or approves a plan: those stay with the operator, and the server enforces it.

## 1. Components

```
 +--------------------------- this Mac ----------------------------------------------+
 |                                                                                    |
 |  Claude Code / Codex  --stdio (MCP)-->  polybench mcp  --HTTP + identity-->  Polybench app (FastAPI, 127.0.0.1:8750)
 |   (reads skills in                      src/polybench/ai/mcp_server.py        |   src/polybench/ai/routes.py   (/api/ai/*)
 |    packaged skills or AGENTS.md)             stdlib only, loopback only            |   src/polybench/ai/guard.py    (identity, allow-list, heartbeat)
 |        |                                                                      |   src/polybench/ai/plan_draft.py (draft, lint, diff, approve)
 |        |                                                                      |   journal.jsonl (ai_action, plan_draft, plan_approved, ...)
 |        v                                                                      |-- plan file  <stem>.md / <stem>.draft.md (next to it)
 |  assistant's cloud service  (outside Polybench: what the assistant reads      |-- ECAD bridge (read / select / clear only)
 |   leaves the Mac here; Polybench itself sends nothing)                        |
 |                                                                               v
 |  Operator  --browser-->  UI  (static/ai_card.js: card 5 + question panel;  static/plan_review.js: step 4 draft review)
 +------------------------------------------------------------------------------------+
```

| Component | File | Does |
|---|---|---|
| MCP server | `src/polybench/ai/mcp_server.py` | JSON-RPC 2.0 over stdio (newline-delimited), MCP methods `initialize`, `tools/list`, `tools/call`, `ping`. Each tool is one HTTP call to the app with the assistant headers. A heartbeat thread posts `/api/ai/heartbeat` every 15 s. Base URL from `--url` or `POLYBENCH_URL`; a non-loopback host is refused. Standard library only. |
| Guard | `src/polybench/ai/guard.py` | Middleware. Reads the identity headers, records the heartbeat, refuses assistant requests outside the allow-list (403), journals refusals. |
| Routes | `src/polybench/ai/routes.py` | `/api/ai/*`: read bundles for the tools, the act routes (propose, note, highlight, ask, plan draft), the operator routes (answer, approve, reject, focus). Adds `S.ai` to `/api/state`. |
| Plan draft | `src/polybench/ai/plan_draft.py` | Draft file, lint with `planfmt.parse`, unified diff, approve (revision row, atomic replace, copy of the old master, journal, regenerate), reject. |
| Mount | `server.py` | One line: `from .ai import mount as _ai_mount; _ai_mount(app, sess)` at the end of `create_app`. |
| CLI | `cli.py` | `polybench mcp` (runs the MCP server), `polybench ai setup` (prints the commands), `polybench ai install-skills [--target DIR] [--link]`. Entry point `polybench-mcp`. |
| Card 5 | `static/ai_card.js` | `renderAiCard(S)` returns the card's HTML from `S.ai`. The file also draws the open-question panel and posts the operator's focus (step, board, test) after each render. |
| Plan review | `static/plan_review.js` | Step 4: draft banner, diff, lint list, Approve / Reject. |
| Skills | `src/polybench/ai/skills/polybench-*/SKILL.md` (package data, so `pip install .` ships them) | bench assistant, test-plan co-authoring, troubleshooting, plugin authoring. |
| Codex context | `AGENTS.md` (repo root) | The same rules in one file. |

## 2. MCP tools

Server name `polybench` (Claude Code shows the tools as `mcp__polybench__<name>`).

| Tool | Kind | HTTP | Returns / does |
|---|---|---|---|
| `get_state` | read | `GET /api/ai/context` | batch, design, simulated, operator, permission mode, boards, steps (state, progress, fails), open stops, open questions, journal head id |
| `get_focus` | read | `GET /api/ai/focus` | the step, board and test on the operator's screen, with the test (limits, class, source, probe, nets, highlight, stop rule, instructions) and the cell |
| `get_board` | read | `GET /api/ai/board?board=` | per step: each test's value, verdict, attempt, pending proposals; stops; similarity outliers; notes; AOI status |
| `get_test` | read | `GET /api/ai/test?test_id=&board=` | test definition, readings across the batch, batch median, similarity flags |
| `get_journal` | read | `GET /api/ai/journal?since=&limit=` | events after an id (max 200) |
| `get_aoi` | read | `GET /api/ai/aoi?board=` | photos (paths), current run, findings with decision, designators, evidence image path; with `thumbnails: true` the MCP server attaches up to `max_images` JPEG thumbnails as MCP image content |
| `get_ecad` | read | `GET /api/ai/ecad?designators=` | ECAD status, BOM parts (line, designators, value, MPN, package, polarity, DNF), nets referenced by the tests and which tests use them |
| `get_plan` | read | `GET /api/ai/plan` | master path, text, hash, lint and coverage; the draft's status |
| `get_polarity` | read | `GET /api/ai/polarity?line=&d=` | the polarity card of a BOM line |
| `troubleshoot` | read | `GET /api/ai/troubleshoot?board=&test_id=` | one bundle for a board (and a test): failing and out-of-band readings with limits and batch, AOI findings and photos on the parts involved, ECAD parts and nets around the probe points, polarity flags, and `missing` (what is not available) |
| `get_questions` | read | `GET /api/ai/questions` | questions asked and their answers |
| `highlight` | act | `POST /api/ai/highlight` | select designators or nets in ECAD (`[]` clears) |
| `propose_value` | act | `POST /api/ai/propose` | posts a proposal on a cell (board, test, value, unit, reason); the operator accepts or rejects it in the UI |
| `add_note` | act | `POST /api/ai/note` | a journal note with `source: assistant` |
| `ask_operator` | act | `POST /api/ai/ask` | a question in the UI (text, optional options, board, test) |
| `write_plan_draft` | act | `POST /api/ai/plan_draft` | writes `<stem>.draft.md` next to the master, lints it, returns the lint and the diff |

Never offered, and refused server-side if attempted directly: accept a result or a proposal, record a FAIL, skip, retest, override a gate, acknowledge a stop, accept or reject a gate proposal, approve or reject a plan, run, answer or cancel an instrument action, safe state, ECAD project selection, BOM cross-check accept, any ECAD edit.

## 3. Journal events

| Event | Payload | Written by |
|---|---|---|
| `ai_action` | `source: assistant`, `client`, `session`, `tool`, `route`, `outcome` (`ok`, `refused`, `error`), `summary`, `board`, `test_id`, `reason` | every assistant act and every refusal |
| `ai_question` | `source: assistant`, `qid`, `text`, `options`, `board`, `test_id` | assistant |
| `ai_answer` | `qid`, `answer`, `dismissed` | operator |
| `plan_draft` | `source: assistant`, `file`, `hash`, `base_hash`, `summary`, `errors`, `warnings` | assistant |
| `plan_approved` | `file`, `old_hash`, `new_hash`, `draft_hash`, `revision`, `summary`, `operator`, `backup` | operator |
| `plan_draft_rejected` | `file`, `hash`, `reason`, `moved_to`, `operator` | operator |
| `proposal` (existing) | `source: {kind: assistant, component: "AI assistant", client, reason}`, `origin: assistant` | assistant |
| `note` (existing) | adds `source: assistant`, `client` | assistant |

Reads are not journaled (they change nothing); the heartbeat is in memory only. An older journal without these events replays unchanged.

## 4. Permission model

### 4.1 Identity

The MCP server sends on every request `X-Polybench-Client: assistant`, `X-Polybench-Assistant: <claude-code|codex|...>` and `X-Polybench-Session: <id of this MCP process>`. The browser UI sends none of them and is the operator.

### 4.2 Modes (one setting)

`POLYBENCH_AI_PERMISSIONS` in the app's environment (default `propose`):

| Mode | Assistant may |
|---|---|
| `off` | nothing: every assistant request is refused (403); the card says the connector is off |
| `read` | the read routes only |
| `propose` (default, PM recommendation, TBR-1) | read, highlight in ECAD, propose a value, add a note, ask a question, write a plan draft |

No mode gives the assistant operator rights. The rule is **deny by default**: an assistant request is allowed only if its method and path are on the allow-list of the mode (`guard.py`, `READ_ROUTES`, `ACT_ROUTES`). Everything else, including every `/api/cmd/*`, `/api/action/*`, `/api/ai/plan_approve`, `/api/ai/plan_reject`, `/api/ai/answer`, the AOI decision and upload routes, the camera stream and voice, is refused with 403 and journaled as `ai_action` outcome `refused`.

### 4.3 The identity is self-declared; the operator session closes the gap (D10, r2)

The assistant identity is a header the client chooses to send, so an assistant that runs `curl -X POST 127.0.0.1:8750/api/cmd/accept` without it would look like the operator's browser. Since r2 every request that is not an assistant request must prove it is the operator's browser session:

- At start the app generates a random operator token (`secrets.token_urlsafe(32)`). `GET /` answers with `Set-Cookie: pb_op=<token>; HttpOnly; SameSite=Strict; Path=/` (not to a request that carries the assistant header).
- Every non-GET request without the assistant header must carry that cookie, else 403 "operator session required". The assistant routes (`/api/ai/propose`, `note`, `highlight`, `ask`, `plan_draft`, `heartbeat`) are on the assistant allow-list and are reached with the assistant header, not the cookie. An assistant-header request is held to the allow-list whatever else it carries.
- The token is held in memory and sent only in that cookie: not in `/api/state`, the journal, the console or any log. `POLYBENCH_OP_TOKEN` in the environment replaces the random token. It is for the test suite and the end-to-end scripts (`tests/e2e/opauth.py`, a test helper), which start their own server; do not set it in normal use. Whenever it is set the app prints a loud warning at startup, and it refuses to start on the default port 8750 (the real bench server) unless `POLYBENCH_OP_TOKEN_ALLOW=1` is also set.
- After a restart the browser's cookie is stale. The page calls `GET /api/session` whenever its event stream (re)connects, which hands it a fresh cookie, and `api()` retries a refused operator POST once after refreshing it (a refused request ran nothing), so no manual reload is needed.
- The CLI never posts to an operator route (`polybench mcp` is an assistant client, `ai setup` and `install-skills` do not talk to the app), so no token file is written. A file under `~/.config/polybench` would be readable by the assistant's shell, which is the thing the cookie keeps out of reach of a careless script.

Trade-off, stated plainly: this stops the accidental bypass and any script that does not know to look. It does not stop an assistant that deliberately runs `curl -i http://127.0.0.1:8750/`, reads the Set-Cookie and replays it: the loopback app cannot tell that process from the browser. A separate OS user or a one-time pairing handshake would. What remains in force for that case is the assistant-side deny rule (Claude Code: a `permissions.deny` entry `Bash(curl*127.0.0.1:8750*)` in the project's `.claude/settings.json`, which the user adds; Polybench does not write it) and the skills and `AGENTS.md`, which forbid calling the HTTP API directly. TBR-2 stays open for the stronger form. Label: HOST-ONLY.

## 5. Test-plan co-authoring flow

1. The assistant reads the master (`get_plan`) and writes the whole new text with `write_plan_draft(text, summary)`.
2. The app writes `<stem>.draft.md` next to the master (never over it), records the master's hash as the draft's base, lints with `planfmt.parse(draft, bom, action ids)`, then loads the draft with the full `protocol.load` (plan override) against the current protocol, so a draft that lints clean but drops a test an action `proposes` is caught, and journals `plan_draft`. The same check lists the test IDs the draft adds and retires. The assistant gets the lint list back and fixes errors with another draft.
3. Design step 4 shows a banner "Draft from the AI assistant", the summary, the lint (errors block), a unified diff against the master, and **Approve** / **Reject**.
4. **Approve** (operator only; operator name required; the draft hash the operator saw is sent back) runs under one lock per session and re-reads everything inside it, so of several simultaneous approves exactly one succeeds. It re-lints, re-runs the full protocol load and refuses with the errors if it fails, checks the master still has the base hash, then (unless the draft already added a revision row with a new `rN` label) appends `| rN | YYYY-MM-DD | <summary> | AI draft approved by <operator> |` to the `## Revision history` table (creates the section if absent), copies the old master to `<batch>/ai/plan_history/<old hash>.md`, writes the new master atomically (temp file + rename), deletes the draft, journals `plan_approved`, and reloads the plan when the protocol generates tests from it.
5. **Reject** (operator only, reason required): the draft moves to `<batch>/ai/rejected/`, journal `plan_draft_rejected`.

## 6. Troubleshooting bundle

`troubleshoot(board, test_id?)`: without a test, every reading on the board that failed, is under review, tripped a stop or is flagged not similar to the batch; with a test, that test. For each: value, limit and class and source, verdict, batch values and median, similarity text. Then the union of the parts (highlight, probe points that are designators) and nets of those tests; the BOM lines of those parts with their polarity flag; the AOI findings of the board on those parts (and all findings of the board as a second list), with evidence paths; the ECAD status; and `missing`: one line per source that is not available. The skill turns this into labelled hypotheses and posts the next measurement with `ask_operator` or `propose_value`.

## 6a. Startup recovery (D1)

If the master plan fails to load at start (`protocol.load` errors), `polybench` looks in `<batch>/ai/plan_history/` for the newest backup that loads, starts on it, leaves the master file untouched, prints the exact `cp` command that restores it, and shows a red banner on every page (`S.ai.recovery`). Approving a corrected draft reloads the master and clears the banner. With no usable backup it prints `polybench plan lint <master> --protocol <file>` and exits 2.

## 6b. Retired tests

`get_test` and `troubleshoot` return the kept result of a test the plan dropped, marked `retired: true`; `get_board` lists them under `retired_results`.

## 7. What the card shows (`S.ai`)

`{mode, connected, client, session, last_seen_s, setup: {claude, codex, skills, url}, actions: [last 30 ai_action/ai_question/plan_* rows], questions: [open], draft: {exists, errors, summary}}`.

## Revision history

| Revision | Date | Change | Source |
|---|---|---|---|
| r1 | 2026-10-17 | First issue. | PM architecture, lead's analysis |
| r2 | 2026-10-17 | Fix round after the gate review: operator session cookie, full-load check and lock on approve, startup recovery, packaged skills, retired results, question strip. | gate review G3/G4 |
