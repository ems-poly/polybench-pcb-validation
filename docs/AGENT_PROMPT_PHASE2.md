# Agent prompt, phase 2: running a linked agent in the background

Status: design note, **not built**. Label: UNVALIDATED (nothing here has been run). Phase 1 (UIR-46) is the **Copy agent prompt** button on design step 4; the text it copies is built by `src/polybench/agent_prompt.py`. This note says how a "Write the plan for me" button could work later, and what it would cost.

## What the operator would see

A second button next to Copy agent prompt: **Run my agent**. Polybench starts the operator's own agent as a child process with the same prompt, shows its output in a log panel, and the usual draft appears in step 4 for review. The operator still presses Approve. Cancel stops the child.

## How it could work

1. **Which agent.** Polybench ships no agent and no model. The operator names a command once in the app settings (workspace file, not the protocol), for example the Claude Code CLI in non-interactive mode or Codex `exec`. Polybench never installs, logs in or configures it (AGENTS.md section 1: it never edits assistant configuration).
2. **Prompt in, tools out.** The child gets the phase 1 prompt on stdin or as its prompt argument. It reaches Polybench only through `polybench mcp`, the same stdio server and the same HTTP guard as today. The MCP configuration is the operator's own; for a trial the existing `polybench ai setup --trial` file can be passed to the child explicitly. Nothing new is allowed: the permission rule (section 2 of AGENTS.md) is unchanged, so the child can only read, highlight, propose, note, ask and `write_plan_draft`.
3. **Process control.** Reuse the action runner pattern (`runner.py`): a child process per run, one run at a time, JSON or text lines to a log, cancel on request, hard timeout, kill of that child's PID only. Start and stop are journalled (`ai_run_started`, `ai_run_ended`, with the command name, exit code and duration; never the prompt body or the model output).
4. **Questions.** The agent's `ask_operator` already lands in the question strip, so a background run can pause for an answer without a chat window.
5. **Result.** Success means a draft exists and lints; it never means the plan is right. The panel says "Draft written, review it" and nothing stronger. A run that wrote no draft, or exited non-zero, is reported as such.

## Risks and the choices they force

| Risk | Choice |
|---|---|
| The child has the operator's shell rights. | Run it with the harness's own sandbox and permission prompts on; Polybench passes no flag that skips them. The operator confirms the command before the first run. |
| Prompt injection from the ECAD, BOM or plan text. | Unchanged from phase 1: outside text is data and is one-lined in the prompt. The server-side guard still refuses every operator route. |
| Data leaves the Mac. | The agent's own cloud service receives what the tools return. The confirm dialog says so in one sentence, before the first run. |
| Runaway or repeated runs. | One run at a time, a wall-clock limit, no automatic re-run. |
| Agent changes between versions. | The command is the operator's; Polybench only checks that it starts and exits. Treat each supported agent as UNVALIDATED until run on a bench Mac. |

## Cost

Engineering (rough, one engineer, no hardware): runner and log panel 2 to 3 days; settings and confirm dialog 1 day; journal events, guard allow-list for the new route (operator-only) and tests with a fake agent script 1 to 2 days; trying it against one real agent per platform 1 day each; docs 0.5 day. About 1.5 to 2 weeks in all. These are estimates, not measurements.

Running cost: Polybench adds none. The operator pays for their own agent's usage. A plan draft is a long write (the whole file) with possible fix rounds after lint errors, so it is a larger job than a chat answer; the actual token use is unknown until measured. TBR: measure tokens per approved plan on the ESP32-S3 example with one agent before putting a number in any marketing copy.

## Recommendation

Do not build until phase 1 has been used on a real board and the operators say the copy-and-paste step is the pain. If it is, build the smallest version: one configurable command, one run, a log panel, cancel, and the journal events above.
