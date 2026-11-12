"""Hands-free voice, end to end at 900 px with recorded clips (macOS `say -o`), simulated instruments, no microphone.

    python tests/e2e/voice_walk.py [shots_dir]        (port: PB_E2E_PORT, default 8829; never 8750)

Env: PB_E2E_REAL_STT=1  use the mlx-whisper backend instead of the <clip>.txt sidecar (run with a python that has mlx-whisper).
     PYTHONPATH         defaults to this checkout's src/.

Server: --simulate, temp data dir, temp voice config (tts_backend log, grace_s 0, llm off). The microphone is never opened: the
Voice switch is not turned on and clips go in through POST /api/voice/clip.

1. "S1 0.2 volts" proposes on S01 for the current test (source voice, shown in the centre editor).
2. "accept" accepts it; "next test" moves the page to the next test.
3. "all boards 2 ohms" proposes on all 10 boards; "accept" accepts the clean ones.
4. A FAIL value is held by "accept" and needs "accept fail".
5. "note R3 is 0603" creates a linked note; "which test" is answered.
6. The Voice tab lists every utterance with route and outcome; each reply is in the transcript and in the journal.
7. Grace: a short grace shows the countdown strip, Esc cancels it, and without Esc the value is accepted.
8. No page errors, no horizontal scroll, screenshots of the Voice tab and the settings panel.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import opauth  # noqa: F401  (operator session cookie, tests/e2e/opauth.py)
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import make_voice_clips  # noqa: E402

PORT = int(os.environ.get("PB_E2E_PORT", "8829"))
BASE = f"http://127.0.0.1:{PORT}/"
PROTOCOL = ROOT / "tests" / "e2e" / "fixtures" / "voice_demo" / "voice_demo.pcbv.yaml"
REAL_STT = os.environ.get("PB_E2E_REAL_STT") == "1"
checks: list[tuple[str, bool]] = []


def check(name, ok, extra=""):
    checks.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""), flush=True)


def get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=30))
    except urllib.error.HTTPError as e:
        return {"_http": e.code}


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode(errors="replace")[:300]}


def until(fn, timeout=20.0, step=0.15):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def main(shots: Path):
    assert PORT != 8750, "8750 is the live server"
    shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pb-voice-e2e-"))
    clips = make_voice_clips.make(tmp / "clips")
    cfgp = tmp / "voice.json"
    cfgp.write_text(json.dumps({"tts_backend": "log", "grace_s": 0, "llm_enabled": False, "stt_backend": "whisper" if REAL_STT else "sidecar"}))
    env = {**os.environ, "POLYBENCH_VOICE_TEST": "1", "POLYBENCH_VOICE_CONFIG": str(cfgp), "PYTHONPATH": os.environ.get("PYTHONPATH") or str(ROOT / "src")}
    srv = subprocess.Popen([sys.executable, "-c", "from polybench.cli import main; main()", "--protocol", str(PROTOCOL), "--simulate", "--data-dir", str(tmp / "data"),
                            "--batch", "VOICE-E2E", "--boards", "10", "--no-browser", "--port", str(PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    errors: list[str] = []
    utts: list[dict] = []
    try:
        check("server up", until(lambda: get("api/state"), 30))
        st = get("api/voice/status")
        check("voice status route answers, stt/llm/tts blocks present", all(k in st for k in ("enabled", "stt", "llm", "tts", "config")), str(st)[:200])
        check("microphone not opened (voice is off)", st.get("enabled") is False and not st.get("recording"))

        def say(name, via="ptt"):
            r = post("api/voice/clip", {"path": str(clips[name]), "via": via})
            if "_http" in r:
                print("clip error", name, r)
            utts.append(r)
            return r

        def cell(b, t):
            return get("api/state")["cells"].get(f"{b}|{t}") or {}

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 900, "height": 1100})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "fonts.g" not in m.text and "ERR_" not in m.text else None)
            ctxs: list[dict] = []
            external: list[str] = []
            page.on("request", lambda rq: external.append(rq.url) if not re.match(r"(https?|wss?)://(127\.0\.0\.1|localhost|\[::1\])[:/]", rq.url) and not rq.url.startswith(("data:", "blob:")) else None)
            page.on("request", lambda rq: ctxs.append(json.loads(rq.post_data or "{}")) if rq.url.endswith("/api/voice/context") else None)
            page.goto(BASE)
            page.wait_for_selector("#timeline .tl")
            check("after a page load the server reports the page visible", until(lambda: (get("api/voice/status").get("context") or {}).get("visible") is True, 5),
                  str(get("api/voice/status").get("context")))
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="V01"]')
            page.click('button[data-act="btab"][data-id="voice"]')
            page.wait_for_selector("#vlist")
            check("Voice tab present with controls", all(page.locator(s).count() == 1 for s in ("#vo-power", "#vo-talk", "#vo-hf", "#vo-meter", "#vo-readouts", "#vo-ack", "#vo-settings", "#vtext")))
            check("page reports its context to the server",   # read back from the server: Playwright's sync request events only fire inside its own calls
                  until(lambda: (get("api/voice/status").get("context") or {}).get("test_id") == "V01"
                        and get("api/voice/status")["context"].get("board") == "S01"), str(get("api/voice/status").get("context")))
            check("current test is V01", page.inner_text("h1.tt .id") == "V01")

            # 0. the page reports its context on every step change, not only when a test is clicked
            ctxnow = lambda: get("api/voice/status").get("context") or {}
            page.click('button.tl[data-id="report"]')
            check("context follows a step change (step report, test cleared)", until(lambda: ctxnow().get("step") == "report" and ctxnow().get("test_id") is None, 5), str(ctxnow()))
            wh = post("api/voice/text", {"text": "which test"})
            check("'which test' after the step change is journalled with the new step", wh.get("step") == "report", str(wh)[:200])
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="V01"]')
            check("context follows back to powered / V01", until(lambda: ctxnow().get("step") == "powered" and ctxnow().get("test_id") == "V01", 5), str(ctxnow()))
            mc = post("api/voice/text", {"text": "can you hear me"})
            check("mic check answers with step and test", mc.get("reply", "").startswith("I can hear you. You are in") and "V01" in mc.get("reply", ""), str(mc.get("reply")))

            # reply voice picker: dropdown, info note, test button; the Open button exists but is not clicked (it would open System Settings)
            page.click("#vo-settings")
            page.wait_for_selector("#vs-voice")
            check("voice picker: first option is the System voice", page.locator("#vs-voice option").first.inner_text().startswith("System voice (set in macOS)"))
            check("info note hidden until the (i) is pressed", not page.locator("#vs-infobox").is_visible())
            page.click("#vs-info")
            note = page.inner_text("#vs-infobox")
            check("info note: Premium voices, no Siri promise, Open button", "Premium voice" in note and "Siri voices are not available" in note and page.locator("#vs-open").is_visible(), note[:160])
            check("Test voice button present", page.locator("#vs-test").count() == 1)
            page.click('[data-vo="vcancel"]')

            # 1. one board
            e1 = say("s1_02v")
            c = until(lambda: (cell("S01", "V01") or {}).get("proposals") and cell("S01", "V01"))
            check("'S1 0.2 volts' proposes on S01 V01", c and len(c["proposals"]) >= 1, str(e1)[:300])
            src = ((c or {}).get("proposals") or [{}])[0].get("source") or {}
            check("proposal source is voice", src.get("kind") == "voice", str(src))
            check("proposal visible in the centre editor", until(lambda: "voice" in page.inner_text("#centre").lower() and "0.2" in page.inner_text("#centre")), page.inner_text("#centre")[-300:])
            check("Voice tab row shows the utterance", until(lambda: re.search(r"S0?1,? 0\.2 volts", page.inner_text("#vlist"))))
            page.screenshot(path=str(shots / "1_proposal.png"))

            # 2. accept, next test
            say("accept")
            check("'accept' accepts the clean proposal", until(lambda: cell("S01", "V01").get("status") == "accepted"), str(cell("S01", "V01"))[:300])
            say("next_test")
            check("'next test' moves the page to R02", until(lambda: page.inner_text("h1.tt .id") == "R02"), page.inner_text("h1.tt .id"))

            # 3. all boards
            say("all_2ohm")
            boards = [f"S{i:02d}" for i in range(1, 11)]
            check("'all boards 2 ohms' proposes on all 10 boards", until(lambda: all(cell(b, "R02").get("proposals") for b in boards)),
                  str({b: bool(cell(b, "R02").get("proposals")) for b in boards}))
            say("accept")
            check("'accept' accepts the clean ones (10 cells)", until(lambda: all(cell(b, "R02").get("status") == "accepted" for b in boards)),
                  str({b: cell(b, "R02").get("status") for b in boards}))

            # 4. FAIL needs accept fail
            say("next_test")
            until(lambda: page.inner_text("h1.tt .id") == "V03")
            say("s1_09v")
            c = until(lambda: cell("S01", "V03").get("proposals") and cell("S01", "V03"))
            check("0.9 V proposed on V03 (verdict fail)", c and any(p.get("verdict") == "fail" for p in c["proposals"]), str(c)[:300])
            e = say("accept")
            time.sleep(0.5)
            check("'accept' holds the FAIL (cell not accepted)", cell("S01", "V03").get("status") != "accepted", str(cell("S01", "V03"))[:300])
            page.screenshot(path=str(shots / "2_fail_held.png"))
            say("accept_fail")
            c = until(lambda: cell("S01", "V03").get("status") == "accepted" and cell("S01", "V03"))
            check("'accept fail' accepts it with verdict fail", c and c.get("verdict") == "fail", str(cell("S01", "V03"))[:300])

            # 5. note, which test
            say("note_r3")
            n = until(lambda: get("api/state")["notes"] or None)
            check("'note R3 is 0603' creates a note linked to R3, the test and the board",
                  n and "R3" in n[-1].get("designators", []) and n[-1].get("test_id") == "V03" and "S01" in n[-1].get("boards", []) and "0603" in n[-1].get("text", ""), str(n)[-300:])
            wt = say("which_test")
            check("'which test' is answered", bool(wt.get("reply")) and "V03" in wt.get("reply", ""), str(wt)[:300])

            # 6. transcript and journal
            utt_ok = [u for u in utts if u.get("utt") is not None]
            check("every clip returned a transcript entry", len(utt_ok) == len(utts), str(utts)[-300:])
            page.click('button[data-act="btab"][data-id="voice"]') if page.locator("#vlist").count() == 0 else None
            until(lambda: page.locator("#vlist .vrow").count() >= len(utt_ok))
            txt = page.inner_text("#vlist")
            check("Voice tab lists every utterance", page.locator("#vlist .vrow").count() >= len(utt_ok), f"{page.locator('#vlist .vrow').count()} of {len(utt_ok)}")
            check("each row carries a route and an outcome badge",
                  page.locator("#vlist .vrow").count() == page.locator("#vlist .vrow .vb.gram, #vlist .vrow .vb.llm").count() == page.locator("#vlist .vrow .vb[class*='out-']").count(),
                  f"rows {page.locator('#vlist .vrow').count()} routes {page.locator('#vlist .vrow .vb.gram, #vlist .vrow .vb.llm').count()} outcomes {page.locator('#vlist .vrow .vb[class*=out-]').count()}")
            check("every TTS reply text is in the transcript", all((u.get("reply") or "") in txt for u in utt_ok) and all(u.get("reply") for u in utt_ok),
                  str([u.get("reply") for u in utt_ok if (u.get("reply") or "") not in txt]))
            jv = [e for e in get("api/journal?limit=1000") if e["type"] == "voice"]
            jr = {e["payload"].get("utt"): e["payload"] for e in jv}
            check("journal has a voice event per utterance with the reply",
                  all(u["utt"] in jr and jr[u["utt"]].get("reply") == u.get("reply") for u in utt_ok), f"{len(jv)} events for {len(utt_ok)} utterances")
            check("snapshot carries the voice history (survives reload)", len(get("api/state").get("voice") or []) >= len(utt_ok))
            page.reload()
            page.wait_for_selector("#timeline .tl")
            page.click('button[data-act="btab"][data-id="voice"]')
            check("after a reload the Voice tab still shows the history", until(lambda: page.locator("#vlist .vrow").count() >= len(utt_ok)))
            check("rows show the parsed values", "values S01 V01 = 0.2 V" in page.inner_text("#vlist") or "values S01 V01 = 0.2" in page.inner_text("#vlist"), page.inner_text("#vlist")[:400])
            page.screenshot(path=str(shots / "3_voice_tab.png"))

            # 5b. an utterance that is not understood in a number test asks a range question; yes records "in range", no number
            cx = {"step": "powered", "test_id": "V01", "board": "S03"}
            q = post("api/voice/text", {"text": "mumble mumble", "context": cx})
            check("range question asks about the limit and the board", q.get("reply") == "Is the reading at most 0.5 volts on S3?" and q.get("outcome") == "question", str(q)[:300])
            y = post("api/voice/text", {"text": "yes", "context": cx})
            c = until(lambda: cell("S03", "V01").get("proposals") and cell("S03", "V01"))
            check("'yes' proposes IN RANGE from the operator, verdict pass", c and c["proposals"][0]["value"] == "IN RANGE" and c["proposals"][0]["origin"] == "operator_confirmed"
                  and c["proposals"][0]["verdict"] == "pass", str(y)[:300])
            check("readback says next test accepts", "Say next test to accept, or reject." in y.get("reply", ""), y.get("reply", ""))
            nt = post("api/voice/text", {"text": "next test", "context": cx})
            c = until(lambda: cell("S03", "V01").get("status") == "accepted" and cell("S03", "V01"))
            check("'next test' accepts it and reads only the next test", c and c.get("value") == "IN RANGE" and nt.get("reply", "").startswith("R02,") and "ccept" not in nt.get("reply", ""), str(nt)[:300])
            page.click('.trow[data-id="V01"]')
            page.click('[data-act="board"][data-id="S03"]')
            check("UI cell shows a tick and (no value)", until(lambda: "in range" in page.inner_text("#centre") and "(no value)" in page.inner_text("#centre")), page.inner_text("#centre")[-300:])
            page.screenshot(path=str(shots / "5b_in_range.png"))

            # typed utterance through the box
            page.fill("#vtext", "which test")
            page.press("#vtext", "Enter")
            check("typed utterance goes through the text box", until(lambda: "typed" in page.inner_text("#vlist")))

            # 7. grace (the reload opened the Project page: the app starts there until the batch has a project)
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="V01"]')
            until(lambda: (get("api/voice/status").get("context") or {}).get("test_id") == "V01")   # the click reaches the server first
            cfg = post("api/voice/config", {"grace_s": 3})
            check("config route applies grace_s", (cfg.get("config") or {}).get("grace_s") == 3, str(cfg)[:200])
            say("s2_02v")
            check("grace: countdown strip shows", until(lambda: page.locator("#vstrip").count() == 1 and "Accepting" in page.inner_text("#vstrip")),
                  str(get("api/voice/status").get("pending")) + " ctx " + str(get("api/voice/status").get("context")) + " last voice: "
                  + str([(v.get("transcript"), v.get("intent"), v.get("values"), v.get("outcome"), v.get("reply")) for v in get("api/state")["voice"][-2:]]))
            page.screenshot(path=str(shots / "4_grace_strip.png"))
            page.keyboard.press("Escape")
            check("Esc cancels the pending accept (strip gone)", until(lambda: page.locator("#vstrip").count() == 0))
            time.sleep(4)
            check("cancelled value stays proposed", cell("S02", "V01").get("status") != "accepted", str(cell("S02", "V01"))[:200])
            cfg = post("api/voice/config", {"grace_s": 1})
            say("s2_02v")
            check("grace: without Esc the clean value is accepted", until(lambda: cell("S02", "V01").get("status") == "accepted", 12), str(cell("S02", "V01"))[:200] + " last voice: " + str([(v.get("transcript"), v.get("intent"), v.get("test_id"), v.get("values"), v.get("outcome"), v.get("reply")) for v in get("api/state")["voice"][-3:]]))

            # 7b. push to talk on Space, the bar, the latch (voice on; POLYBENCH_VOICE_TEST=1 swaps the microphone for a fake with a steady level)
            vstat = lambda: get("api/voice/status")
            check("a second client that is hidden does not mute the visible page",
                  post("api/voice/visibility", {"visible": False, "client": "headless-closed"}).get("context", {}).get("visible") is True)
            check("PTT bar hidden while Voice is off", not page.locator("#pttbar").is_visible())
            post("api/voice/enable", {"on": True})
            page.reload()
            page.wait_for_selector("#timeline .tl")
            check("after a reload the status is visible and the fake microphone is open", until(lambda: vstat()["context"]["visible"] is True and vstat()["listening"] is True, 8), str(vstat())[:300])
            page.click('button.tl[data-id="powered"]')
            page.click('.trow[data-id="V01"]')
            page.wait_for_selector("#val")
            check("PTT bar visible whenever Voice is on (Voice tab not open)", until(lambda: page.locator("#ptt-bar").is_visible()) and page.locator("#vlist").count() == 0)
            check("hint under the bar", "Hold ` to talk (or Space, outside a text box)" in page.inner_text("#ptt-hint"), page.inner_text("#ptt-hint"))
            fillw = lambda: page.evaluate("parseFloat(document.getElementById('ptt-fill').style.width) || 0")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            page.keyboard.down("Space")
            check("Space held outside the inputs starts PTT", until(lambda: vstat()["recording"] is True, 5), str(vstat())[:200])
            check("bar says Listening... and shows the level while held", until(lambda: "Listening" in page.inner_text("#ptt-label") and fillw() > 30), f"{page.inner_text('#ptt-label')} {fillw()}")
            page.screenshot(path=str(shots / "7_ptt_space.png"))
            time.sleep(2.6)
            check("a long hold keeps recording (heartbeat)", vstat()["recording"] is True)
            page.keyboard.up("Space")
            check("releasing Space stops recording", until(lambda: vstat()["recording"] is False, 5))
            check("bar says Transcribing... then shows a result", until(lambda: "Transcribing" in page.inner_text("#ptt-label") or "Nothing heard" in page.inner_text("#ptt-label") or "\u2014" in page.inner_text("#ptt-label"), 3), page.inner_text("#ptt-label"))
            check("bar settles on a result", until(lambda: page.inner_text("#ptt-label") not in ("Listening\u2026", "Transcribing\u2026"), 8), page.inner_text("#ptt-label"))
            page.click("#val")
            page.keyboard.type("1")
            page.keyboard.press("Space")
            page.keyboard.type("2")
            check("Space inside #val types", page.input_value("#val") == "1 2" and vstat()["recording"] is False, page.input_value("#val"))
            page.fill("#val", "")
            page.keyboard.down("Backquote")
            check("the ` key inside #val starts PTT and does not type", until(lambda: vstat()["recording"] is True, 5) and page.input_value("#val") == "")
            page.keyboard.up("Backquote")
            check("releasing ` stops it", until(lambda: vstat()["recording"] is False, 5))
            page.click("#val")
            bb = page.locator("#ptt-bar").bounding_box()
            page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            page.mouse.down()
            check("click and hold on the bar starts PTT from inside #val", until(lambda: vstat()["recording"] is True, 5))
            check("focus stays in #val while the bar is held", page.evaluate("document.activeElement.id") == "val")
            check("bar animates with the level while held with the mouse", until(lambda: fillw() > 30), str(fillw()))
            page.mouse.up()
            check("releasing the bar stops recording", until(lambda: vstat()["recording"] is False, 5))
            page.click("#ptt-latch")
            check("latch turns hands-free on, bar says Hands-free", until(lambda: vstat()["handsfree"] is True and "Hands-free" in page.inner_text("#ptt-label"), 5), page.inner_text("#ptt-label"))
            page.evaluate("document.activeElement && document.activeElement.blur()")
            page.keyboard.down("Space")
            time.sleep(0.6)
            check("while latched Space does nothing", vstat()["recording"] is False)
            page.keyboard.up("Space")
            page.screenshot(path=str(shots / "8_ptt_latched.png"))
            page.click("#ptt-latch")
            check("latch off again", until(lambda: vstat()["handsfree"] is False, 5))
            time.sleep(7)           # let the result line expire so the later layout checks see the idle bar

            # 8. settings panel, layout
            page.click('button[data-act="btab"][data-id="voice"]')
            page.click("#vo-settings")
            page.wait_for_selector("#vo-sform")
            until(lambda: page.locator("#vs-model option").count() >= 1)
            check("settings shows the local-only privacy line", "Voice: local only: STT" in page.inner_text("#vs-priv"), page.inner_text("#vs-priv"))
            check("settings panel has the fields", all(page.locator(s).count() == 1 for s in ("#vs-llm", "#vs-model", "#vs-voice", "#vs-rate", "#vs-grace", "#vs-gap", "#vs-key", "#vs-row")))
            page.fill("#vs-grace", "2")
            page.screenshot(path=str(shots / "5_settings.png"))
            page.click("#vs-save")
            check("settings save through POST /api/voice/config", until(lambda: get("api/voice/status")["config"].get("grace_s") == 2))
            check("settings dialog closed", until(lambda: page.locator("#vo-sform").count() == 0))
            check("no horizontal scroll at 900 px", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth && document.getElementById('bottom').scrollWidth <= document.getElementById('bottom').clientWidth + 1"),
                  page.evaluate("[document.documentElement.scrollWidth, window.innerWidth, document.getElementById('bottom').scrollWidth, document.getElementById('bottom').clientWidth]"))
            page.evaluate("document.documentElement.dataset.theme='dark'; ui.theme='dark'")
            page.screenshot(path=str(shots / "6_voice_tab_dark.png"))
            check("no page errors", not errors, "; ".join(errors[:5]))
            check("the page made no request off this Mac (fonts vendored)", not external, "; ".join(external[:5]))
            browser.close()
    finally:
        srv.terminate()
        try:
            out = srv.communicate(timeout=10)[0].decode(errors="replace")
        except Exception:
            srv.kill()
            out = ""
        if any(not ok for _, ok in checks):
            print("---- server log tail ----\n" + out[-2500:])
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed; STT backend: {'whisper' if REAL_STT else 'sidecar'}; shots: {shots}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "e2e" / "shots_voice"))
