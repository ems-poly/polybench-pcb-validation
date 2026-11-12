from polybench.engine import normalise

T = {"kind": "number", "unit": "kOhm"}


def test_ol_oc_sc_tokens():
    for tok in ("OL", "ol", "OC", "oc", "open"):
        r = normalise(T, tok)
        assert r["ok"] and r["num"] >= 1e15 and r["value"] == tok.upper()
    for tok in ("SC", "sc", "short"):
        r = normalise(T, tok)
        assert r["ok"] and r["num"] == 0.0 and r["value"] == tok.upper()
    assert not normalise(T, "XX")["ok"]
    assert normalise(T, "4.7")["num"] == 4.7
