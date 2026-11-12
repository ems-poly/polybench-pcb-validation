# Privacy and data

**Why it matters:** prototype design data, BOMs and test results are usually confidential to you or your client. Polybench is built so that none of it leaves your machine.

## Nothing leaves the machine

- The server listens on **127.0.0.1** only. Other computers on the network cannot reach it.
- The user interface loads nothing from the internet. Its fonts (IBM Plex Sans and Mono, SIL Open Font Licence) ship with the app.
- The ECAD bridge talks to the ECAD program's local server on 127.0.0.1.
- Instruments are reached directly: USB for the programmer, the CAN adapter and cameras; your local network for a LAN instrument such as the power analyzer.
- [Voice](voice.md) runs speech to text from the local model cache and refuses any language-model address that is not on the loopback interface.
- The test suite includes a check that fails if any source file names a non-loopback web address (licence files excepted).
- There is no account, no telemetry and no update check.

Internet is needed only to install Polybench and its optional models.

## What is stored where

| What | Where |
|---|---|
| Your protocol, test plan, BOM and AOI layout | Where you keep them. Polybench reads them and never edits them. |
| Everything about a batch | `~/polybench-data/<batch>/` (change with `--data-dir` or `POLYBENCH_DATA`) |
| Instrument state | `~/.cache/polybench/` (for example the last power-analyzer connection) |
| Voice settings | `~/.config/polybench/voice.json` |
| Camera scanner settings | `~/Library/Application Support/Polybench/AssemblyScanner/` (macOS) |
| Speech and language models | The Hugging Face cache and Ollama's own folder |

### Inside a batch folder

```text
~/polybench-data/DEMO-B1/
  journal.jsonl            every event, append-only, flushed to disk before the screen shows it
  runs/NNNN_<action>/      evidence of each action run: CSV captures, JSON, images
  reports/                 report.md, report.html, errata_candidates.md
  aoi/photos/              board photos you uploaded
  aoi/removed/             photos you removed, kept with the reason
  aoi/labels.jsonl         defects you added by hand, for later training
  voice-clips/             recorded voice clips (WAV)
  polarity_cache.json      the last good footprint reads for the polarity card
```

Everything is plain files: JSON lines, CSV, markdown, HTML and images. You can read, copy, archive or delete them with ordinary tools.

## What the journal contains

The operator name you typed, time stamps, every value and its source, notes, scan codes, file names and hashes of photos and evidence, and voice transcripts. It does not contain design files or datasheets.

## Sharing a report

`report.html` and `report.md` are self-contained summaries; evidence files stay in `runs/` and are not copied into the report. Check the report before you send it: it names the design, the batch, the operator and any notes you wrote.

## Simulated data

A batch started with `--simulate` is labelled SIMULATED in the app, the journal and the report, so simulated results cannot be mistaken for measurements.
