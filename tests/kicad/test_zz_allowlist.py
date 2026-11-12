"""KC-33: runs last in this folder; every command type the whole KiCad suite put on the (fake) wire is on the allow-list."""
from polybench.kicad import guard


def test_commands_seen_by_the_suite_are_allowed():
    assert guard.SEEN, "the suite sent nothing?"
    assert guard.SEEN <= guard.ALLOWED | {"RunAction"}, sorted(guard.SEEN - guard.ALLOWED)
    assert not (guard.SEEN & {"SaveDocument", "RevertDocument", "BeginCommit", "EndCommit", "CreateItems", "UpdateItems", "DeleteItems",
                              "InteractiveMoveItems", "FlipItems", "SaveCopyOfDocument", "RefillZones"})
    assert guard.SEEN_ACTIONS <= guard.ALL_ACTION_NAMES, sorted(guard.SEEN_ACTIONS - guard.ALL_ACTION_NAMES)
