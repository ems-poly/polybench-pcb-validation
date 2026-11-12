"""Test plan viewer: loader key, markdown subset, Obsidian wikilinks and comments, sanitising."""
import re

from polybench.protocol import load
from polybench.testplan import load_plan, render_markdown


def R(md, sib=lambda n: None):
    return render_markdown(md, sib)["html"]


# ---- loader
def _proto(demo_copy, extra):
    f = demo_copy / "demo.pcbv.yaml"
    f.write_text(f.read_text(encoding="utf-8") + "\n" + extra + "\n", encoding="utf-8")
    return load(f)


def test_no_key_no_data(demo_copy):
    assert "test_plan" not in load(demo_copy / "demo.pcbv.yaml").data


def test_key_resolves_relative_to_top_file(demo_copy):
    (demo_copy / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (demo_copy / "more.md").write_text("# More\n", encoding="utf-8")
    p = _proto(demo_copy, "test_plan: plan.md\ntest_plan_files: [more.md]")
    assert not p.errors and not p.warnings
    fs = p.data["test_plan"]["files"]
    assert [f["name"] for f in fs] == ["plan.md", "more.md"] and all(f["exists"] for f in fs)
    assert fs[0]["path"] == str((demo_copy / "plan.md").resolve())


def test_missing_file_is_warning_not_error(demo_copy):
    p = _proto(demo_copy, "test_plan: ../nope/DEMO_TEST_PLAN.md")
    assert not p.errors
    assert any("test plan file not found" in str(w) for w in p.warnings)
    assert p.data["test_plan"]["files"][0]["exists"] is False


def test_wrong_type_is_error(demo_copy):
    assert any("test_plan_files" in str(e) for e in _proto(demo_copy, "test_plan_files: 3").errors)


# ---- sanitising
def test_script_does_not_survive():
    h = R("hello <script>alert(1)</script> <img src=x onerror=alert(2)>\n\n<iframe src=//x></iframe>\n\n[x](javascript:alert(3)) [y](http://a.b/?q=\"><b>)")
    tags = set(re.findall(r"<(/?[a-z0-9]+)", h))
    assert tags <= {"p", "/p", "span", "/span", "a", "/a"}, tags            # only tags the renderer writes itself
    assert "&lt;script&gt;" in h and "&lt;img" in h and "&lt;iframe" in h
    assert 'href="javascript' not in h and '"><b>' not in h


def test_code_block_is_escaped():
    h = R("```html\n<script>x()</script>\n```")
    assert "<script" not in h and "&lt;script&gt;" in h


# ---- comments and wikilinks
def test_comments_stripped_incl_multiline_but_not_in_code():
    h = R("keep %% inline note %% this\n\n%% multi\nline\nnote %%\n\nafter\n\n```\n%% stays %%\n```")
    assert "inline note" not in h and "multi" not in h and "keep" in h and "after" in h
    assert "%% stays %%" in h


def test_wikilinks_plain_label_or_link():
    h = R("see [[Other Note]] and [[Other Note|the label]] and [[Missing|gone]] and [[Other Note#Sec]]", lambda n: "Other Note.md" if n == "Other Note" else None)
    assert 'data-tp-open="Other Note.md"' in h and ">the label</a>" in h
    assert '<span class="wl">gone</span>' in h
    assert "[[" not in h and "<a href=\"#\" class=\"tp-link\" data-tp-open=\"Other Note.md\">Other Note</a>" in h
    h2 = R("[[X]] [[a/b/Y|why]]")
    assert "<a" not in h2 and '<span class="wl">X</span>' in h2 and ">why</span>" in h2


def test_front_matter_dropped_and_properties_returned():
    r = render_markdown("---\ndate: 2026-10-16\nstatus: draft\n---\n# T\n")
    assert r["properties"] == {"date": "2026-10-16", "status": "draft"} and "<h1>T</h1>" in r["html"] and "draft" not in r["html"]


# ---- structure
def test_table_list_callout_code():
    h = R("""# Title\n\n| a | b |\n|:--|--:|\n| 1 | `x\\|y` |\n| 2 | **bold** |\n\n- one\n  - nested\n- [x] done\n\n1. first\n2. second\n\n> [!warning] Mind the heat\n> Hot **iron**.\n\n```python\nprint(1)\n```\n""")
    assert "<h1>Title</h1>" in h and "<table>" in h and h.count("<tr>") == 3 and 'text-align:right' in h
    assert "<code>x|y</code>" in h
    assert "<ul>" in h and h.count("<ul>") == 2 and "<ol>" in h
    assert 'callout c-warning' in h and "Mind the heat" in h and "<strong>iron</strong>" in h
    assert 'data-lang="python"' in h


# ---- file facts
def test_load_plan_facts_and_sibling_guard(tmp_path):
    (tmp_path / "plan.md").write_text("# P\n[[side]]\n", encoding="utf-8")
    (tmp_path / "side.md").write_text("# Side\n", encoding="utf-8")
    r = load_plan(tmp_path / "plan.md")
    assert r["exists"] and r["mtime"] and 'data-tp-open="side.md"' in r["html"]
    assert load_plan(tmp_path / "plan.md", "side.md")["exists"]
    assert not load_plan(tmp_path / "plan.md", "../x.md")["exists"]
    assert not load_plan(tmp_path / "gone.md")["exists"]


def test_git_commit_shown_for_tracked_file(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "plan.md").write_text("# P\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first"], check=True)
    g = load_plan(tmp_path / "plan.md")["git"]
    assert g["commit"] and g["subject"] == "first" and not g["dirty"]
    (tmp_path / "plan.md").write_text("# P2\n", encoding="utf-8")
    assert load_plan(tmp_path / "plan.md")["git"]["dirty"]
