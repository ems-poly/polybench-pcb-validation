# Polybench documentation site

The public user manual for Polybench, built with MkDocs and the Material theme. Source pages are in `docs/`; the build goes to `_build/` (not committed).

## Build and preview locally

Use a separate virtual environment for the site tooling:

```bash
python3.12 -m venv /tmp/pbsite-venv
/tmp/pbsite-venv/bin/pip install -r site/requirements.txt
/tmp/pbsite-venv/bin/mkdocs build --strict -f site/mkdocs.yml     # must pass with no warnings
/tmp/pbsite-venv/bin/mkdocs serve -f site/mkdocs.yml -a 127.0.0.1:8782
```

The site loads nothing from a CDN: system fonts, local search.

## Screenshots

Every screenshot is taken from Polybench running the synthetic DEMO-1 board with `--simulate`, on its own port, with a scratch data folder:

```bash
PB_DOCS_FORBIDDEN="name1,name2" <venv>/bin/python site/tools/capture_screenshots.py \
    --polybench <venv>/bin/polybench --port 8781
```

The venv needs Polybench with the `scanner`, `aoi` and `voice` extras and Playwright. `PB_DOCS_FORBIDDEN` lists names that must never appear in a public screenshot; the script checks each page's text and stops on a hit. The protocol used is `src/polybench/examples/demo-docs/demo-docs.pcbv.yaml` (the shipped DEMO-1 fragments plus a test plan and a synthetic AOI layout).

## Publishing

`.github/workflows/pages.yml` builds the site on every change and deploys it to GitHub Pages only when the repository variable `POLYBENCH_PAGES` is `true` and Pages is set to "GitHub Actions".
