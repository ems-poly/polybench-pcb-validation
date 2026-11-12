"""pytest plugin: fixtures of the ported Assembly Scanner tests."""
import pytest

from asc_support import need


@pytest.fixture(scope="session")
def demo_files(tmp_path_factory):
    """The generated DEMO purchasing BOMs and bag-label images (tests/assembly_scanner/demo_fixtures.py), built once per run.
    Skips where the scanner extra (openpyxl, Pillow, pillow-heif, zxing-cpp) is not installed."""
    for m in ("openpyxl", "PIL", "pillow_heif", "zxingcpp", "cv2", "numpy"):
        pytest.importorskip(m)
    import demo_fixtures as D
    d = tmp_path_factory.mktemp("asc_demo")
    return {"basket": D.build_basket_bom(d / D.BASKET_NAME), "order": D.build_order_bom(d / D.ORDER_NAME), **D.build_labels(d / "labels")}


@pytest.fixture(scope="session")
def bom_a(demo_files):
    """The DEMO basket workbook (Basket, Tuning kit, Consumables, Parts mapping)."""
    return need("polybench.assembly_scanner.bom").load_bom(str(demo_files["basket"]))


@pytest.fixture(scope="session")
def bom_b(demo_files):
    """The DEMO BOM workbook with an Order sheet."""
    return need("polybench.assembly_scanner.bom").load_bom(str(demo_files["order"]))


@pytest.fixture(scope="session")
def demo_bom():
    """A synthetic BOM of the DEMO designators (one placement per line), for the board-choice tests."""
    from asc_fake_fusion_mcp import DEMO_DESIGNATORS
    M = need("polybench.assembly_scanner.models")
    return M.Bom([M.BomLine(f"DEMO-{i}", None, (d,), None, None, "BOM", i) for i, d in enumerate(DEMO_DESIGNATORS, 2)], "demo")


@pytest.fixture
def fake_fusion():
    from asc_fake_fusion_mcp import FakeFusionMCP
    with FakeFusionMCP() as fake:
        yield fake


# The PoC's working-tree FusionClient (a later rewrite of the vendored client) reads the board list and designators through
# fusion_mcp_read / fusion_mcp_electronics_read, which answer for the ACTIVE editor only. These tests were written for the
# earlier all-boards script path (designators of every open board in one call, four tool calls per highlight, readOnly on
# every call). The behaviour they pin down was retired on purpose in the PoC; they stay in as xfail so the difference is visible.
_RETIRED = {
    "test_auto_select_clear_winner": "the client scores only the active board's designators",
    "test_ambiguous_tie_no_highlight_and_warns": "non-active documents have no designators to tie on",
    "test_fresh_client_defaults_to_auto": "two open boards: the client now refuses until one is chosen",
    "test_select_board_none_returns_to_auto": "two open boards: the client now refuses until one is chosen",
    "test_override_not_persisted_across_instances": "two open boards: the client now refuses until one is chosen",
    "test_highlight_uses_real_handshake_and_carries_designators": "read tools are sent without readOnly",
    "test_every_operation_is_read_only": "a highlight is now 11 tool calls, not 4",
}


def pytest_collection_modifyitems(items):
    for it in items:
        why = _RETIRED.get(it.name)
        if why and "assembly_scanner" in str(it.fspath):
            it.add_marker(pytest.mark.xfail(reason="PoC client changed: " + why, strict=False))
