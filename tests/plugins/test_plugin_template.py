"""examples/plugin-template run through the real child runner (skipped unless it is pip-installed)."""
import pytest
from pb_plugin_helpers import run_child

from polybench import registry


def test_installed_template_runs_as_the_core_runs_it(tmp_path):
    if "example.echo" not in registry.action_names():
        pytest.skip("examples/plugin-template is not pip-installed in this environment")
    msgs, _, rc = run_child("example.echo", {"map": {"reading": "T01"}}, tmp_path)
    assert rc == 0 and any(m["t"] == "proposal" for m in msgs) and msgs[-1]["t"] == "done"
