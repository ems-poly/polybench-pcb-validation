# Getting support

Polybench is maintained by **one person, on a best-effort basis**. There is no guaranteed response time and no paid support.

## Where to go

| You have | Go to |
|---|---|
| A usage question or how-to | GitHub Discussions, Q&A category |
| A bug | A Bug report issue, with `polybench doctor --json` output |
| An instrument or tool that should be supported | A Plug-in request issue |
| A result with a specific instrument (works or fails) | A Hardware report |
| A security or instrument-safety problem | A private report, see [SECURITY.md](SECURITY.md) |

## Help me help you

- Reproduce with `--simulate` if you can, and say whether real hardware was involved.
- Include `polybench doctor --json` output.
- Remove client data, board files, photographs and serial numbers before posting. Issues are public.

## Automated replies

New issues get automatic labels, and sometimes one request for doctor output, from an AI assistant (Claude). Its analyses and reviews are advisory and can be wrong. A human makes every decision and merges every change. Issue text is sent to Anthropic's API for this.

## Not in scope

Custom test-plan development, support for closed instruments without a public programming manual, and private debugging sessions. Windows is not supported.
