# polybench-plugin-example

A minimal third-party plug-in for Polybench: one action, `example.echo`, no hardware. Copy this folder, rename it and replace
`plugin.py`. Read "Sharing a plug-in" in `docs/PLUGINS.md` first.

```
pip install -e examples/plugin-template
polybench plugins list          # example.echo appears, UNVALIDATED (no recorded readiness)
pytest examples/plugin-template/tests
```

Files: `pyproject.toml` (the entry point under `polybench.actions`), `src/polybench_plugin_example/plugin.py` (the action,
`selftest()`, `safe_state()`, the simulate path), `tests/`. The template contains no client data; keep yours the same.
