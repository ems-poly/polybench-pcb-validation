"""AI connector: lets Claude Code or Codex work beside the operator through an MCP server (mcp_server.py) that calls
these routes. Mounted by one line in server.py. See docs/AI_CONNECTOR_ARCHITECTURE.md."""
from __future__ import annotations


def mount(app, sess):
    """Install the guard and the routes once. Returns attach(session): call it for the first session and for every session the
    Project page opens later (it gives the session its hub and the S.ai part of /api/state). `sess` is the live proxy and may point
    at no session yet (welcome page)."""
    from . import guard, routes
    hub = guard.Hub(sess)
    guard.install(app, hub)
    routes.install(app, sess, hub)

    def attach(s):
        s.ai_hub = hub
        orig = s.snapshot

        def snapshot():                   # /api/state carries S.ai for card 5, without touching Session.snapshot
            out = orig()
            try:
                out["ai"] = routes.card_state(s, hub)
            except Exception as e:        # the card must never break the main screen
                out["ai"] = {"mode": hub.mode, "error": str(e)}
            return out
        s.snapshot = snapshot
    return attach
