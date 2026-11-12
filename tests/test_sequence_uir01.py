"""UIR-01: the Program-and-function sequence (DEMO protocol, --simulate; HOST-ONLY).

R1 a failed final flash is retried alone (same attempt, no new `start`); R2 a passed equipment check is reused; R3 a failed equipment
check asks once and never loops."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from polybench.protocol import load
from polybench.server import Session
from polybench.engine import CmdError

EX = Path(__file__).resolve().parents[1] / "src/polybench/examples/demo_automated"
STEP = "function"


def make(tmp_path, missing_pcan=False, flaky_final=True):
    d = tmp_path / "proto"
    shutil.copytree(EX, d)
    f = d / "function.yaml"
    t = f.read_text()
    if missing_pcan:
        t = t.replace("        bitrate: 1000000\n", "        bitrate: 1000000\n        sim: {status: missing, detail: \"PCAN-USB not on USB\", hint: \"plug it in\"}\n", 1)
    if flaky_final:
        t = t.replace("        plugin: openocd.flash\n        final: true\n        needs: [probe]\n        params: {image_from: final_images, map: {image_sha256: A90}}",
                      f"        plugin: demo.fail\n        final: true\n        needs: [probe]\n        params: {{until_file: {tmp_path / 'probe_ok'}, propose: {{A90: abc}}}}")
        assert "demo.fail" in t
    f.write_text(t)
    sess = Session(load(d / "demo_automated.pcbv.yaml"), tmp_path / "data", "b", True, 3, None, operator="Op")
    return sess


async def until(pred, what="condition", t=8.0):
    for _ in range(int(t / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for " + what)


def events(sess, phase=None):
    return [e["payload"] for e in sess.engine.journal.events if e["type"] == "sequence" and (phase is None or e["payload"].get("phase") == phase)]


async def drive(sq, board="S01", skip_bench_confirm=False):
    """Run one sequence to its end as the operator would: confirm the bench if it asks, answer yes to the question."""
    await sq.start(STEP, board, True)
    while sq.active:
        await asyncio.sleep(0.02)
        ask = (sq.live or {}).get("ask")
        if ask and ask["kind"] == "bench" and not skip_bench_confirm:
            await sq.bench_reply("confirm", {}, via="ui")
        elif ask and ask["kind"] == "check":
            await sq.answer("yes")


def test_r1_retry_final_only(tmp_path):
    async def go():
        sess = make(tmp_path)
        sq = sess.sequencer
        await drive(sq)
        rec = sess.engine.state.sequences[("S01", STEP)]
        assert rec["outcome"] == "complete"
        att = rec["attempt"]
        starts0 = len(events(sess, "start"))
        accepted0 = {r["test"]: r["value"] for r in sess.engine.seq_rows("S01", STEP) if r["src"] == "accepted"}
        await sq.proceed(STEP, "S01", True, None)
        await until(lambda: not sq.active, "first final flash")
        rec = sess.engine.state.sequences[("S01", STEP)]
        assert rec["final"]["state"] == "error" and "programmer not found" in rec["final"]["text"]
        # the old route is closed: only a retest could run again
        with pytest.raises(CmdError):
            await sq.proceed(STEP, "S01", True, None)
        # retry while it still fails: same attempt, same record, no `start`
        r = await sq.retry_final(STEP, "S01")
        assert r["attempt"] == att and r["retry"] == 2
        await until(lambda: not sq.active, "retry")
        assert sess.engine.state.sequences[("S01", STEP)]["final"]["state"] == "error"
        (tmp_path / "probe_ok").write_text("x")           # the operator plugged the probe in
        await sq.retry_final(STEP, "S01")
        await until(lambda: not sq.active, "second retry")
        rec = sess.engine.state.sequences[("S01", STEP)]
        assert rec["final"]["state"] == "ok"
        assert len(events(sess, "start")) == starts0, "a retry must not open a sequence"
        assert rec["attempt"] == att == sess.engine.state.attempt("S01", STEP)
        assert [e["try_no"] for e in events(sess, "final")] == [1, 2, 3]
        assert all(e["attempt"] == att for e in events(sess, "final"))
        now = {r["test"]: r["value"] for r in sess.engine.seq_rows("S01", STEP) if r["src"] == "accepted"}
        assert all(now[k] == v for k, v in accepted0.items()), "accepted results must stay"
        with pytest.raises(CmdError):                      # nothing left to retry
            await sq.retry_final(STEP, "S01")
    asyncio.run(go())


def test_r1_retry_refused_when_not_applicable(tmp_path):
    async def go():
        sess = make(tmp_path)
        sq = sess.sequencer
        with pytest.raises(CmdError):                      # no run at all
            await sq.retry_final(STEP, "S01")
        await drive(sq)
        with pytest.raises(CmdError):                      # verdict not accepted yet
            await sq.retry_final(STEP, "S01")
    asyncio.run(go())


def test_r2_second_sequence_reuses_the_check(tmp_path):
    async def go():
        sess = make(tmp_path, flaky_final=False)
        sq = sess.sequencer
        await drive(sq, "S01")
        assert STEP in sq.bench_cache_status()
        asks0 = [e for e in events(sess, "bench") if not e.get("reused")]
        assert len(asks0) == 1
        await drive(sq, "S02", skip_bench_confirm=True)        # would hang if it asked
        rec = sess.engine.state.sequences[("S02", STEP)]
        assert rec["outcome"] == "complete"
        b = [e for e in events(sess, "bench") if e.get("reused")]
        assert len(b) == 1 and b[0]["seq"] == rec["seq"]
        txt = next(i["text"] for i in rec["items"] if i["id"] == "_bench")
        assert "reused" in txt
        assert sess.snapshot()["bench_cache"][STEP]["age_s"] >= 0
    asyncio.run(go())


def test_r2_reuse_ends_on_window_usb_change_or_action_error(tmp_path):
    async def go():
        sess = make(tmp_path, flaky_final=False)
        sq = sess.sequencer
        await drive(sq, "S01")
        sq._bench_cache[STEP]["ts"] -= sq.bench_reuse_s + 1             # window over
        assert STEP not in sq.bench_cache_status()
        await sq.start(STEP, "S02", True)
        await until(lambda: (sq.live or {}).get("ask", {}) and sq.live["ask"]["kind"] == "bench", "bench asks again")
        await sq.bench_reply("confirm", {}, via="ui")
        sq.request_abort()
        await until(lambda: not sq.active, "abort")
        # USB list changed since the check: not reused
        sq.usb_fingerprint = lambda: (("1", "2", "a"),)
        sq._remember_bench(STEP, [e for e in sess.proto.step(STEP)["equipment"]], sq._bench_cache[STEP]["results"], (("1", "2", "a"),))
        sq.usb_fingerprint = lambda: (("1", "2", "a"), ("1", "3", "b"))
        await sq.start(STEP, "S03", True)
        await until(lambda: (sq.live or {}).get("ask", {}) and sq.live["ask"]["kind"] == "bench", "bench asks after a USB change")
        sq.request_abort()
        await until(lambda: not sq.active, "abort 2")
    asyncio.run(go())


def test_r2_final_flash_error_drops_the_kept_check(tmp_path):
    async def go():
        sess = make(tmp_path)
        sq = sess.sequencer
        await drive(sq)
        assert STEP in sq.bench_cache_status()
        await sq.proceed(STEP, "S01", True, None)
        await until(lambda: not sq.active)
        assert STEP not in sq.bench_cache_status()
    asyncio.run(go())


def test_r2_check_again_before_a_run_refreshes_or_drops(tmp_path):
    async def go():
        sess = make(tmp_path, flaky_final=False)
        sq = sess.sequencer
        r = await sq.bench_check_only(STEP)
        assert r["kept"] and STEP in sq.bench_cache_status()
        await drive(sq, "S01", skip_bench_confirm=True)                     # reuses the pre-run check: no ask
        assert [e for e in events(sess, "bench")][0].get("reused")
        eq = sess.proto.step(STEP)["equipment"]
        sq._bench_cache[STEP]["sig"] = "other"                              # the equipment list changed
        assert await sq._reusable_bench(STEP, [e for e in eq]) is None
    asyncio.run(go())


def test_r3_missing_equipment_asks_once(tmp_path):
    async def go():
        sess = make(tmp_path, missing_pcan=True)
        sq = sess.sequencer
        asks = []
        sq.listeners.append(lambda k, i: asks.append((k, i)) if k == "ask" else None)
        sq.usb_poll_s = 0.05
        await sq.start(STEP, "S01", True)
        await until(lambda: asks, "the bench question")
        await asyncio.sleep(1.0)                                            # no operator input: it must stay quiet
        assert len(asks) == 1 and "missing" in asks[0][1]["question"]
        assert "check again" not in asks[0][1]["question"].lower() and "recheck" not in asks[0][1]["question"].lower()
        assert len(events(sess, "bench")) == 1
        with pytest.raises(CmdError):                                        # a missing required item cannot be confirmed
            await sq.bench_reply("confirm", {}, via="ui")
        assert len(asks) == 1
        # voice cannot answer its own prompt: a recheck that is not the screen button is held off right after a check
        with pytest.raises(CmdError):
            await sq.bench_reply("recheck", {})
        assert len(asks) == 1 and len(events(sess, "bench")) == 1
        # the operator presses Check again: one more check, one more question
        await sq.bench_reply("recheck", {}, via="ui")
        await until(lambda: len(asks) == 2, "the second question")
        await asyncio.sleep(0.5)
        assert len(asks) == 2 and len(events(sess, "bench")) == 2
        assert [e["trigger"] for e in events(sess, "bench")] == ["operator", "operator"]
        sq.request_abort()
        await until(lambda: not sq.active)
    asyncio.run(go())


def test_r3_usb_change_rechecks_once(tmp_path):
    async def go():
        sess = make(tmp_path, missing_pcan=True)
        sq = sess.sequencer
        asks = []
        sq.listeners.append(lambda k, i: asks.append(i) if k == "ask" else None)
        PEAK = ("3186", "6", "PCAN-USB")
        state = {"fp": (("1", "2", "hub"),)}
        sq.usb_fingerprint = lambda: state["fp"]
        sq.usb_poll_s = 0.05
        await sq.start(STEP, "S01", True)
        await until(lambda: asks, "question")
        await asyncio.sleep(0.4)
        assert len(asks) == 1                                               # same device list: silent
        state["fp"] = (("1", "2", "hub"), PEAK)                              # the adapter was plugged in
        await until(lambda: len(asks) == 2, "re-asked after the USB change")
        await asyncio.sleep(0.4)
        assert len(asks) == 2                                               # once, not a loop
        assert [e["trigger"] for e in events(sess, "bench")] == ["operator", "usb"]
        sq.request_abort()
        await until(lambda: not sq.active)
    asyncio.run(go())


# ---- QA defects D1 to D5 (UIR-01 QA)
async def accepted_then_failed_final(tmp_path):
    sess = make(tmp_path)
    sq = sess.sequencer
    await drive(sq)
    await sq.proceed(STEP, "S01", True, None)
    await until(lambda: not sq.active, "first final flash")
    assert sess.engine.state.sequences[("S01", STEP)]["final"]["state"] == "error"
    return sess, sq


def test_d1_retry_refused_after_retest(tmp_path):
    async def go():
        sess, sq = await accepted_then_failed_final(tmp_path)
        sess.engine.retest("S01", STEP, cause="operator retest")
        with pytest.raises(CmdError) as e:
            await sq.retry_final(STEP, "S01")
        assert e.value.status == 409 and "attempt" in str(e.value)
        assert len(events(sess, "final")) == 1
    asyncio.run(go())


def test_d2_retry_refused_when_accepted_results_changed(tmp_path):
    async def go():
        sess, sq = await accepted_then_failed_final(tmp_path)
        sess.engine.entry("S01", "A02", "0xdeadbeef", replace=True, confirm=True)
        with pytest.raises(CmdError) as e:
            await sq.retry_final(STEP, "S01")
        assert e.value.status == 409 and "A02" in str(e.value)
        assert len(events(sess, "final")) == 1
    asyncio.run(go())


def test_d2_unchanged_results_still_retry(tmp_path):
    async def go():
        sess, sq = await accepted_then_failed_final(tmp_path)
        await sq.retry_final(STEP, "S01")
        await until(lambda: not sq.active)
    asyncio.run(go())


def test_d3_unrelated_usb_flapping_never_re_asks(tmp_path):
    async def go():
        sess = make(tmp_path, missing_pcan=True)
        sq = sess.sequencer
        asks = []
        sq.listeners.append(lambda k, i: asks.append(i) if k == "ask" else None)
        n = {"i": 0}
        sq.usb_fingerprint = lambda: (("1", "2", "hub"),) + ((("9", "9", "flap"),) if n["i"] % 2 else ())
        sq.usb_poll_s = 0.02
        await sq.start(STEP, "S01", True)
        await until(lambda: asks, "question")
        for _ in range(40):
            n["i"] += 1
            await asyncio.sleep(0.03)
        assert len(asks) == 1
        sq.request_abort()
        await until(lambda: not sq.active)
    asyncio.run(go())


def test_d3_wanted_device_flapping_is_rate_limited(tmp_path):
    async def go():
        sess = make(tmp_path, missing_pcan=True)
        sq = sess.sequencer
        asks = []
        sq.listeners.append(lambda k, i: asks.append(i) if k == "ask" else None)
        n = {"i": 0}
        peak = ("3186", "6", "PCAN-USB")
        sq.usb_fingerprint = lambda: (("1", "2", "hub"),) + ((peak,) if n["i"] % 2 else ())
        sq.usb_poll_s = 0.02
        sq.usb_recheck_min_s = 10.0
        await sq.start(STEP, "S01", True)
        await until(lambda: asks, "question")
        for _ in range(60):
            n["i"] += 1
            await asyncio.sleep(0.03)
        assert len(asks) <= 2, f"{len(asks)} asks in 2 s"
        sq.request_abort()
        await until(lambda: not sq.active)
    asyncio.run(go())


def test_d4_retry_bad_image_is_422_not_500(tmp_path):
    async def go():
        sess, sq = await accepted_then_failed_final(tmp_path)
        with pytest.raises(CmdError) as e:
            await sq.retry_final(STEP, "S01", {"source": "protocol", "index": 99})
        assert e.value.status == 422
        with pytest.raises(CmdError) as e:
            await sq.retry_final(STEP, "S01", {"source": "protocol", "index": "x"})
        assert e.value.status == 422
        sess.engine.state.sequences[("S01", STEP)]["image"] = {"source": "protocol", "label": "renamed away"}
        with pytest.raises(CmdError) as e:
            await sq.retry_final(STEP, "S01")
        assert e.value.status == 422 and "no longer listed" in str(e.value)
    asyncio.run(go())


def test_d5_retry_final_has_an_operator_only_reason():
    from polybench.ai.guard import OPERATOR_ONLY_HINT
    assert "operator-only" in OPERATOR_ONLY_HINT["/api/sequence/retry_final"]


def test_item1_successful_retry_restores_the_kept_check(tmp_path):
    async def go():
        sess, sq = await accepted_then_failed_final(tmp_path)
        assert STEP not in sq.bench_cache_status()
        await sq.retry_final(STEP, "S01")                    # still failing: stays dropped
        await until(lambda: not sq.active)
        assert STEP not in sq.bench_cache_status()
        (tmp_path / "probe_ok").write_text("x")
        await sq.retry_final(STEP, "S01")
        await until(lambda: not sq.active)
        assert sess.engine.state.sequences[("S01", STEP)]["final"]["state"] == "ok"
        assert STEP in sq.bench_cache_status()
        await drive(sq, "S02", skip_bench_confirm=True)       # would hang if it asked
        assert [e for e in events(sess, "bench") if e["seq"] == sess.engine.state.sequences[("S02", STEP)]["seq"]][0]["reused"] is True
    asyncio.run(go())


def test_item2_check_pressed_for_this_board_is_not_called_reused(tmp_path):
    async def go():
        sess = make(tmp_path, flaky_final=False)
        sq = sess.sequencer
        await sq.bench_check_only(STEP, (), "S01")
        await drive(sq, "S01", skip_bench_confirm=True)
        rec = sess.engine.state.sequences[("S01", STEP)]
        txt = next(i["text"] for i in rec["items"] if i["id"] == "_bench")
        assert "reused" not in txt and "checked" in txt
        assert events(sess, "bench")[0]["reused"] is False
    asyncio.run(go())
