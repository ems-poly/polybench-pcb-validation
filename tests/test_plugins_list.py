"""D-1, D-2, D-3: `polybench plugins list`, the generated docs/SUPPORTED_EQUIPMENT.md, the plug-in template."""
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from polybench import plugins_cmd, registry

REPO = Path(__file__).resolve().parents[1]


def _core_names():
    return {r["name"] for r in plugins_cmd._rows(core_only=True)}


def test_every_core_plugin_listed_with_a_label():
    rows = plugins_cmd._rows(core_only=True)
    names = {r["name"] for r in rows}
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    eps = pyproject["project"]["entry-points"]
    declared = set(eps["polybench.actions"]) | set(eps["polybench.ecad"]) | set(registry.BUILTIN)
    assert declared <= names
    assert all(r["label"] in plugins_cmd.LABELS for r in rows)


def test_readiness_table_has_no_stale_keys_and_never_exceeds_docs():
    assert set(plugins_cmd.READINESS) <= _core_names()
    doc = (REPO / "docs" / "PLUGINS.md").read_text()
    # Only these were ever recorded HARDWARE-VALIDATED (at least in part); nothing else may claim it.
    allowed = {"fusion", "kicad", "n6705b.identify"}
    hv = {k for k, (lab, _) in plugins_cmd.READINESS.items() if lab == "HARDWARE-VALIDATED"}
    assert hv <= allowed
    for k in hv:
        assert f"`{k}`" in doc


def test_unrecorded_plugin_is_unvalidated():
    assert plugins_cmd.READINESS.get("not.recorded") is None
    label, _ = plugins_cmd.READINESS.get("not.recorded", ("UNVALIDATED", ""))
    assert label == "UNVALIDATED"


def test_cli_text_and_md():
    out = subprocess.run([sys.executable, "-m", "polybench.cli", "plugins", "list"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "kicad" in out.stdout and "HARDWARE-VALIDATED" in out.stdout
    md = subprocess.run([sys.executable, "-m", "polybench.cli", "plugins", "list", "--md"], capture_output=True, text=True, timeout=30)
    assert md.stdout.startswith("| Plug-in |")


def test_supported_equipment_doc_is_not_stale():
    on_disk = (REPO / "docs" / "SUPPORTED_EQUIPMENT.md").read_text()
    assert on_disk == plugins_cmd.equipment_doc(), (
        "docs/SUPPORTED_EQUIPMENT.md is stale; run: polybench plugins list --md --core > docs/SUPPORTED_EQUIPMENT.md")


def test_docs_have_sharing_sections():
    assert "## Sharing a plug-in" in (REPO / "docs" / "PLUGINS.md").read_text()
    sk = (REPO / "src/polybench/ai/skills/polybench-plugin-authoring/SKILL.md").read_text()
    assert "## Sharing a plugin" in sk and "examples/plugin-template" in sk


def test_template_package_is_wellformed_and_runs(tmp_path):
    t = REPO / "examples" / "plugin-template"
    py = tomllib.loads((t / "pyproject.toml").read_text())
    ep = py["project"]["entry-points"]["polybench.actions"]
    assert ep == {"example.echo": "polybench_plugin_example.plugin:ExampleEcho"}
    sys.path.insert(0, str(t / "src"))
    try:
        from polybench_plugin_example.plugin import ExampleEcho
        assert ExampleEcho().selftest() is True
    finally:
        sys.path.remove(str(t / "src"))


def test_installed_template_registers():
    if "example.echo" not in registry.action_names():
        pytest.skip("examples/plugin-template is not pip-installed in this environment")
    row = next(r for r in plugins_cmd._rows() if r["name"] == "example.echo")
    assert row["label"] == "UNVALIDATED" and row["package"] == "polybench-plugin-example"


def _clash_eps(monkeypatch, names_by_group):
    """Register the template's class under core names, as a third-party package with the same entry-point name would."""
    from types import SimpleNamespace
    real = registry._all_eps

    def fake(group):
        extra = []
        for n in names_by_group.get(group, []):
            extra.append(SimpleNamespace(name=n, value="polybench_plugin_example.plugin:ExampleEcho", group=group,
                                         dist=SimpleNamespace(name="polybench-plugin-example"),
                                         load=lambda: __import__("polybench_plugin_example.plugin", fromlist=["x"]).ExampleEcho))
        return extra + real(group)           # the third-party entry point comes FIRST: it must still lose
    monkeypatch.setattr(registry, "_all_eps", fake)


def test_name_clash_core_wins_and_third_party_is_unvalidated_and_flagged(monkeypatch):
    t = REPO / "examples" / "plugin-template" / "src"
    monkeypatch.syspath_prepend(str(t))
    _clash_eps(monkeypatch, {"polybench.actions": ["n6705b.identify"], "polybench.ecad": ["kicad"]})
    registry._warned.clear()
    with pytest.warns(UserWarning, match="shadowed"):
        act = registry.find_action("n6705b.identify")
    assert type(act).__module__.startswith("polybench.plugins.n6705b")       # the core class runs
    rows = plugins_cmd._rows()
    for name, kind in (("n6705b.identify", "action"), ("kicad", "ecad")):
        mine = [r for r in rows if r["name"] == name and r["kind"] == kind]
        core = next(r for r in mine if not r["shadowed"])
        shad = next(r for r in mine if r["shadowed"])
        assert core["package"] == "polybench" and core["label"] == "HARDWARE-VALIDATED"
        assert shad["package"] == "polybench-plugin-example" and shad["label"] == "UNVALIDATED"
        assert "SHADOWED" in shad["evidence"]
    assert "SHADOWED" in plugins_cmd.render_text(rows)
    # the generated core document is unaffected by third-party packages
    assert plugins_cmd.equipment_doc() == (REPO / "docs" / "SUPPORTED_EQUIPMENT.md").read_text()
