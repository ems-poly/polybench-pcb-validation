"""UI-3: several open Fusion boards. Auto-pick the board that best covers the BOM's
placements, allow an override, never highlight silently on an ambiguous board, and a pinned
board that gets closed is an error (never a fallback to another board).

Written against FusionClient.list_boards() -> [board dict with key/name/...],
select_board(key|name|None) and auto_select_for(designators) -> (best, coverage, runner_up),
where best["confident"] says whether it became the auto target.
"""
from __future__ import annotations

import json

import pytest

from asc_support import need
from asc_fake_fusion_mcp import (FakeFusionMCP, SimulatedFusion, DEMO_DESIGNATORS,
                                   DEMO_B_DESIGNATORS)
from test_asc_fusion_client_fake import assert_loud_failure

BOARD_A = "DEMO-1 v1"
BOARD_A_COPY = "DEMO-1 v1 (copy)"
VIB = "DEMO-2 v3"


def _best(x):
    """auto_select_for returns (best, coverage, runner_up); best is only a pick if confident."""
    return x[0] if isinstance(x, tuple) else x


def _name(x):
    x = _best(x)
    if isinstance(x, dict) and x.get("confident") is False:
        return None
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("name", "doc_name", "board", "picked", "selected"):
            if isinstance(x.get(k), str) or (k in x and x[k] is None):
                return x[k]
        return None
    return getattr(x, "name", None)


def _warning(x) -> str:
    x = _best(x)
    if isinstance(x, dict):
        return " ".join(str(x.get(k) or "") for k in ("warning", "error", "reason", "message"))
    return " ".join(str(getattr(x, k, "") or "") for k in ("warning", "error", "reason"))


def board_names(client) -> list[str]:
    return [_name(b) for b in client.list_boards()]


def key_of(client, name: str):
    """select_board() takes the board key from list_boards() (lineage URN), not the doc name."""
    for b in client.list_boards():
        if _name(b) == name:
            if isinstance(b, dict):
                return b.get("key", name)
            return getattr(b, "key", name)
    pytest.fail(f"{name!r} not in list_boards()")


def pick_name(client, pick):
    """auto_select_for may return a key, a name or a record: map it to the doc name."""
    n = _name(pick)
    if n is None:
        return None
    for b in client.list_boards():
        k = b.get("key") if isinstance(b, dict) else getattr(b, "key", None)
        if n in (_name(b), k):
            return _name(b)
    return n


def auto_select(client, demo_bom):
    """Try the designator-set form first, then the Bom form."""
    fn = client.auto_select_for
    placements = {d for ln in demo_bom.lines if ln.fitted for d in ln.designators}
    try:
        return fn(placements)
    except (TypeError, AttributeError):
        return fn(demo_bom)


def client_for(url):
    fc = need("polybench.assembly_scanner.fusion_client")
    c = fc.FusionClient(url)
    for m in ("list_boards", "select_board", "auto_select_for"):
        if not hasattr(c, m):
            pytest.fail(f"FusionClient.{m}() has not landed yet", pytrace=False)
    return c


@pytest.fixture
def two_boards():
    """Two boards open; board B is the active tab, so the right pick is
    not simply 'whatever is active'."""
    f = SimulatedFusion(boards={BOARD_A: DEMO_DESIGNATORS, VIB: DEMO_B_DESIGNATORS},
                        active_doc=VIB)
    with FakeFusionMCP(fusion=f) as fake:
        yield fake


# ---- the fake itself -------------------------------------------------------------------
def test_fake_exposes_all_open_boards():
    f = SimulatedFusion(boards={BOARD_A: DEMO_DESIGNATORS, VIB: DEMO_B_DESIGNATORS},
                        active_doc=VIB)
    src = ("import adsk.core, adsk.electron\n"
           "def run(_c):\n"
           "    app = adsk.core.Application.get()\n"
           "    for d in app.documents:\n"
           "        bd = adsk.electron.BoardDocument.cast(d)\n"
           "        print(bd.name, bd.board.elements.count, bd.isActive)\n")
    out = json.loads(f.run_script(src))["message"].splitlines()
    assert out == [f"{BOARD_A} {len(DEMO_DESIGNATORS)} False",
                   f"{VIB} {len(DEMO_B_DESIGNATORS)} True"]
    f.close_board(BOARD_A)
    assert list(f.boards) == [VIB]


# ---- FusionClient ------------------------------------------------------------------------
def test_list_boards(two_boards):
    assert sorted(board_names(client_for(two_boards.url))) == sorted([BOARD_A, VIB])


def test_auto_select_clear_winner(two_boards, demo_bom):
    c = client_for(two_boards.url)
    assert pick_name(c, auto_select(c, demo_bom)) == BOARD_A
    r = c.highlight(["R4", "R5"])
    assert r.ok
    assert two_boards.fusion.selected_on[BOARD_A] == ("R4", "R5")
    assert two_boards.fusion.selected_on[VIB] == (), "highlighted on the wrong board"


def test_single_board_is_picked(demo_bom):
    with FakeFusionMCP() as fake:
        c = client_for(fake.url)
        assert pick_name(c, auto_select(c, demo_bom)) == BOARD_A


def test_ambiguous_tie_no_highlight_and_warns(demo_bom):
    f = SimulatedFusion(boards={BOARD_A: DEMO_DESIGNATORS,
                                BOARD_A_COPY: DEMO_DESIGNATORS}, active_doc=BOARD_A)
    with FakeFusionMCP(fusion=f) as fake:
        c = client_for(fake.url)
        pick = auto_select(c, demo_bom)
        assert _name(pick) is None, f"tie must not auto-pick, got {pick!r}"
        assert _warning(pick).strip() or pick is None
        assert_loud_failure(lambda: c.highlight(["R4", "R5"]))
        assert f.any_selected() == {}, "highlighted on an ambiguous board"


def test_override_wins_over_auto_pick(two_boards, demo_bom):
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    c.select_board(key_of(c, VIB))
    assert c.highlight(["R4"]).ok
    assert two_boards.fusion.selected_on[VIB] == ("R4",)
    assert two_boards.fusion.selected_on[BOARD_A] == ()


def test_pinned_board_closed_is_error_not_fallback(two_boards):
    c = client_for(two_boards.url)
    c.select_board(key_of(c, BOARD_A))
    assert c.highlight(["R4"]).ok
    two_boards.fusion.close_board(BOARD_A)
    assert_loud_failure(lambda: c.highlight(["R4", "R5"]))
    assert two_boards.fusion.selected_on[VIB] == (), "fell back to another board"


def test_status_names_the_board_in_use(two_boards, demo_bom):
    """UI-2 needs the board name; the client must report the one it highlights on."""
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    st = c.status()
    blob = json.dumps(st, default=str)
    assert BOARD_A in blob, f"status() does not name the selected board: {st}"


# ---- UI-3 detail (user, 7 Oct): Auto on every launch; override is session-only -------------
def test_fresh_client_defaults_to_auto(two_boards, demo_bom):
    """A new client with no override follows the auto pick (board A), not the active tab (board B)."""
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    assert c.highlight(["R4", "R5"]).ok
    assert two_boards.fusion.selected_on[BOARD_A] == ("R4", "R5")
    assert two_boards.fusion.selected_on[VIB] == ()


def test_override_survives_multiple_scans(two_boards, demo_bom):
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    c.select_board(key_of(c, VIB))
    for desig in (["R4"], ["C2", "C3"], ["IC2"], ["R8"]):
        auto_select(c, demo_bom)          # the app may re-run auto on BOM reload: override holds
        assert c.highlight(desig).ok
        assert two_boards.fusion.selected_on[VIB] == tuple(desig)
    assert two_boards.fusion.selected_on[BOARD_A] == ()


def test_select_board_none_returns_to_auto(two_boards, demo_bom):
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    c.select_board(key_of(c, VIB))
    c.highlight(["R4"])
    c.select_board(None)
    assert c.highlight(["R4", "R5"]).ok
    assert two_boards.fusion.selected_on[BOARD_A] == ("R4", "R5"), "did not return to the auto pick"


def test_override_not_persisted_across_instances(two_boards, demo_bom, tmp_path, monkeypatch):
    """Same config dir, new instance -> Auto again; nothing on disk remembers the override."""
    for var in ("HOME", "XDG_CONFIG_HOME", "ASCAN_CONFIG_DIR"):
        monkeypatch.setenv(var, str(tmp_path))
    c1 = client_for(two_boards.url)
    auto_select(c1, demo_bom)
    c1.select_board(key_of(c1, VIB))
    c1.highlight(["R4"])
    try:
        cfg = need("polybench.assembly_scanner.config")
        for fn in ("save", "save_config"):
            if callable(getattr(cfg, fn, None)):
                try:
                    getattr(cfg, fn)()
                except TypeError:
                    pass
    except BaseException:       # app.config not landed yet: the disk scan below still applies
        pass
    vib_key = SimulatedFusion.lineage(VIB)
    leaked = [p for p in tmp_path.rglob("*") if p.is_file()
              and any(t in p.read_text(encoding="utf-8", errors="ignore") for t in (VIB, vib_key))]
    assert not leaked, f"override persisted to {leaked}"
    c2 = client_for(two_boards.url)
    auto_select(c2, demo_bom)
    assert c2.highlight(["R4", "R5"]).ok
    assert two_boards.fusion.selected_on[BOARD_A] == ("R4", "R5"), "new instance kept the override"


def test_overridden_doc_closed_is_error_no_fallback(two_boards, demo_bom):
    c = client_for(two_boards.url)
    auto_select(c, demo_bom)
    c.select_board(key_of(c, VIB))
    assert c.highlight(["R4"]).ok
    two_boards.fusion.close_board(VIB)
    assert_loud_failure(lambda: c.highlight(["R4", "R5"]))
    assert two_boards.fusion.selected_on[BOARD_A] == (), "fell back to the auto/other board"


def test_app_settings_never_store_a_board_choice(tmp_path):
    """UI-3: Settings (config.json) has no board field, and a stale board key in a config
    file is ignored on load (a relaunch is always Auto)."""
    import dataclasses
    main = need("polybench.assembly_scanner.main")
    names = {f.name for f in dataclasses.fields(main.Settings)}
    assert not {n for n in names if "board" in n or "target" in n or "pin" in n}, names
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"bom_path": "x.xlsx", "board": "urn:x", "pinned_board": "urn:x"}))
    st = main.Settings.load(cfg)
    assert st.bom_path == "x.xlsx"
    assert st.save(cfg) is None
    assert "urn:x" not in cfg.read_text()
