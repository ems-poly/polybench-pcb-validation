# Ported from the earlier voice prototype prg/grammar.py. Changes for Polybench: _board_at rewritten (S1, S01, S 1, S-1, es one, board ten, boards one to five, boards 1 and 2, up to 40 boards; B forms kept).
"""Deterministic grammar parser for spoken (or typed) bench utterances.

The parser is independent of any sheet except for the set of known row IDs, which it needs to
recognise "see oh seven" as C07. It returns a plain dict; binding to cells and unit checks are done
by `validate.py`. Nothing here guesses: anything left over after the grammar has consumed what it
understands makes the parse fail with a reason.
"""
from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from . import units as un

DIGIT_WORDS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
               "seven": 7, "eight": 8, "nine": 9, "to": 2, "too": 2, "for": 4, "fore": 4, "won": 1, "tree": 3}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
# words that may mean a digit only in a numeric context (homophones); never taken as a number on their own
HOMOPHONES = {"to", "too", "for", "fore", "won", "tree", "o"}
LETTER_NAMES = {"ay": "a", "bee": "b", "be": "b", "see": "c", "sea": "c", "dee": "d", "eff": "f", "gee": "g",
                "aitch": "h", "jay": "j", "kay": "k", "el": "l", "em": "m", "en": "n", "pee": "p", "pea": "p",
                "queue": "q", "cue": "q", "are": "r", "ar": "r", "ess": "s", "tee": "t", "tea": "t",
                "you": "u", "vee": "v", "ex": "x", "why": "y", "zee": "z", "zed": "z"}
FILLERS = {"uh", "um", "er", "please", "the", "is", "reads", "read", "equals", "equal", "at", "of", "its", "it's",
           "value", "reading", "measured", "that", "then", "and", "i", "got", "has", "for", "on", "now", "so",
           "okay", "alright"}

ACCEPT = {"accept", "accepted", "confirm", "confirmed", "approve"}
REJECT = {"reject", "discard", "cancel", "delete", "clear"}
UNDO = {"undo"}
SKIP = {"skip"}
NEXT = {"next"}
BACK = {"previous", "back"}
REPEAT = {"repeat", "again"}
OL_WORDS = {"ol", "overrange", "overload", "overflow"}
OK_WORDS = {"ok", "okay", "good", "fine"}
YES = {"yes", "yeah", "yep", "yup"}
NO = {"no", "nope"}


def _tokens(text: str) -> tuple[list[str], list[str]]:
    t = text.replace("Ω", " ohm ").replace("Ω", " ohm ").replace("µ", "u").replace("μ", "u")
    t = t.replace("%", " percent ")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)            # 1,200 -> 1200
    t = re.sub(r"[;:!?\"()\[\]$]", " ", t)
    t = re.sub(r"[,]", " ", t)
    t = re.sub(r"(?<![\d.])-(?=\d)", " minus ", t)
    t = re.sub(r"(?<=[a-zA-Z])-(?=[a-zA-Z0-9])", " ", t)  # five-volt, C-07
    t = re.sub(r"(?<=\d)-(?=\d)", " ", t)                  # 5-0 (hex digits read as a range)
    t = re.sub(r"(?<=\d)-(?=[a-zA-Z])", " ", t)
    t = re.sub(r"\.(?=\s|$)", " ", t)                    # trailing sentence full stops
    orig = []
    for tok in t.split():
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
            orig.append(tok)
        else:
            orig.extend(re.sub(r"(?<=\d)(?=[a-zA-Z])", " ", tok).split())   # 412ohm -> 412 ohm; 07a -> 07 a
    return [o.lower() for o in orig], orig


# ---- numbers -------------------------------------------------------------------------------

def _is_digits(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?", tok))


def _int_words(toks: list[str], i: int, allow_homophone: bool = True):
    """Parse an integer from words/digits at toks[i:]. Returns (int, next_i) or None."""
    n = len(toks)
    j = i
    total = 0
    cur = 0
    got = False
    while j < n:
        t = toks[j]
        if t == "and" and got and j + 1 < n and (toks[j + 1] in TENS or toks[j + 1] in TEENS or
                                                 (toks[j + 1] in DIGIT_WORDS and toks[j + 1] not in HOMOPHONES)
                                                 or toks[j + 1].isdigit()):
            j += 1
            continue
        if re.fullmatch(r"\d+", t) and not (got and cur % 10 and False):
            if got and not (j > i and toks[j - 1] in ("hundred", "thousand", "and")) and cur:
                break
            cur += int(t)
            got = True
        elif t in TEENS:
            if got and cur % 100:
                break
            cur += TEENS[t]
            got = True
        elif t in TENS:
            if got and cur % 100:
                break
            cur += TENS[t]
            got = True
        elif t in DIGIT_WORDS and (t not in HOMOPHONES or allow_homophone):
            if got and cur % 10 and cur % 100 < 20 and cur % 100 != 0 and (cur % 100) % 10 != 0:
                break
            if got and cur % 100 in range(1, 10):
                break
            cur += DIGIT_WORDS[t]
            got = True
        elif t == "hundred" and got:
            cur = (cur if cur else 1) * 100 if cur < 100 else cur * 100
            got = True
        elif t == "thousand" and got:
            total += (cur if cur else 1) * 1000
            cur = 0
        else:
            break
        j += 1
    if not got and total == 0:
        return None
    return total + cur, j


def parse_number(toks: list[str], i: int):
    """Parse a (possibly decimal, signed) number. Returns (Decimal, next_i) or None."""
    n = len(toks)
    j = i
    sign = 1
    if j < n and toks[j] in ("minus", "negative"):
        sign, j = -1, j + 1
    if j < n and re.fullmatch(r"[-+]?\d+(\.\d+)?", toks[j]) and not (j + 1 < n and toks[j + 1] in ("hundred", "thousand")):
        # plain numeric token, optionally followed by "point digits"
        val = Decimal(toks[j])
        j += 1
        if "." not in toks[j - 1] and j < n and toks[j] == "point":
            frac = _frac_digits(toks, j + 1)
            if frac:
                s, j = frac
                val = Decimal(f"{toks[j - 1 - len(s.split()) ] if False else int(val)}.{s}") if val >= 0 else val
        return sign * val, j
    ip = None
    if j < n and toks[j] != "point":
        ip = _int_words(toks, j, allow_homophone=False if toks[j] in HOMOPHONES else True)
    if ip is None and not (j < n and toks[j] == "point"):
        # digit-by-digit: "four one two"
        k = j
        digs = ""
        while k < n and (toks[k] in DIGIT_WORDS and toks[k] not in HOMOPHONES or re.fullmatch(r"\d", toks[k])):
            digs += str(DIGIT_WORDS.get(toks[k], toks[k]))
            k += 1
        if digs:
            return sign * Decimal(digs), k
        return None
    if ip is not None:
        val, j = Decimal(ip[0]), ip[1]
        # digit-by-digit continuation, e.g. "four one two" -> only when integer was one single-digit word
        if j < n and toks[j] != "point":
            k = j
            if val < 10 and ip[1] - i == 1 and toks[i] in DIGIT_WORDS:
                digs = str(int(val))
                while k < n and (toks[k] in DIGIT_WORDS and toks[k] not in HOMOPHONES or re.fullmatch(r"\d", toks[k])):
                    digs += str(DIGIT_WORDS.get(toks[k], toks[k]))
                    k += 1
                if k > j:
                    val, j = Decimal(digs), k
    else:
        val = Decimal(0)
    if j < n and toks[j] == "point":
        frac = _frac_digits(toks, j + 1)
        if frac:
            s, j2 = frac
            val = Decimal(f"{int(val)}.{s}")
            j = j2
        elif ip is None:
            return None
    return sign * val, j


def _frac_digits(toks: list[str], i: int):
    n = len(toks)
    j = i
    s = ""
    while j < n:
        t = toks[j]
        if re.fullmatch(r"\d+", t):
            s += t
        elif t in DIGIT_WORDS and t not in HOMOPHONES:
            s += str(DIGIT_WORDS[t])
        else:
            break
        j += 1
    return (s, j) if s else None


# ---- ID matching ---------------------------------------------------------------------------

def canon(s: str) -> str:
    s = s.lower().replace("_", "").replace("-", "").replace(" ", "")
    return re.sub(r"(?<!\d)0+(?=\d)", "", s)


# spoken unit words as they appear inside snake_case IDs (iin_5v, d3_drop_180ma, fsw_khz)
UNIT_TOKEN = {"volt": "v", "volts": "v", "amp": "a", "amps": "a", "milliamp": "ma", "milliamps": "ma",
              "millivolt": "mv", "millivolts": "mv", "microamp": "ua", "microamps": "ua", "kilohertz": "khz",
              "megahertz": "mhz", "microsecond": "us", "microseconds": "us", "millisecond": "ms",
              "milliseconds": "ms", "kilohertz.": "khz"}


def _token_pieces(tok: str) -> list[str]:
    out = []
    if tok in LETTER_NAMES:
        out.append(LETTER_NAMES[tok])
    if tok in DIGIT_WORDS and tok not in ("to", "too", "for", "fore", "won", "tree"):
        out.append(str(DIGIT_WORDS[tok]))
    if tok in TEENS:
        out.append(str(TEENS[tok]))
    if tok in TENS:
        out.append(str(TENS[tok]))
    if tok in ("point", "dot"):
        out.append(".")
    if tok in ("underscore", "dash", "hyphen", "minus"):
        out.append("")
    if "_" in tok and re.fullmatch(r"[a-z0-9_]+", tok):
        out.append(tok.replace("_", ""))
    if tok in UNIT_TOKEN:
        out.append(UNIT_TOKEN[tok])
    if re.fullmatch(r"[a-z0-9]+(\.\d+)?", tok):
        out.append(tok)
    return out


def _seq_variants(toks: tuple[str, ...]) -> set[str]:
    n = len(toks)

    @lru_cache(maxsize=None)
    def rec(k: int) -> frozenset:
        if k == n:
            return frozenset({""})
        res = set()
        for p in _token_pieces(toks[k]):
            for tail in rec(k + 1):
                res.add(p + tail)
        # multi-word number, e.g. "fourteen" handled above; "twenty two" -> 22
        for m in range(k + 2, min(n, k + 4) + 1):
            ip = _int_words(list(toks[k:m]), 0)
            if ip and ip[1] == m - k:
                for tail in rec(m):
                    res.add(str(ip[0]) + tail)
        return frozenset(res)
    return set(rec(0))


class IdIndex:
    """Known row IDs. `match` finds one in a token list.

    Full match: the spoken form equals the ID with underscores and leading zeros ignored
    ("see oh seven" = C07, "sens G" = SENS_G).
    Spoken alias (snake_case IDs only): the first word of the ID plus any ordered subset of its other
    words and numbers, so "ripple one eighty" finds ripple_pp_mv_180ma and "d3 drop one hundred"
    finds d3_drop_100ma. An alias that fits more than one ID is returned as ambiguous and is rejected
    by the binder, never resolved by guessing.
    """

    def __init__(self, ids):
        self.map: dict[str, set[str]] = {}
        self.partial: dict[str, set[str]] = {}
        for i in ids:
            self.map.setdefault(canon(i), set()).add(i)
        for i in set(ids):
            if "_" not in i:
                continue
            parts = [p.lower() for p in re.findall(r"[A-Za-z]+|\d+", i)][:10]
            for mask in range(1 << (len(parts) - 1)):
                al = parts[0] + "".join(p for k, p in enumerate(parts[1:]) if mask >> k & 1)
                if len(al) >= 3:
                    self.partial.setdefault(canon(al), set()).add(i)

    def match(self, toks: list[str], i: int):
        n = len(toks)
        for j in range(min(n, i + 10), i, -1):
            vs = _seq_variants(tuple(toks[i:j]))
            for v in vs:
                c = canon(v)
                if c in self.map:
                    return sorted(self.map[c])[0], j, sorted(self.map[c])
            for v in vs:
                c = canon(v)
                if c in self.partial:
                    ids = sorted(self.partial[c])
                    return ids[0], j, ids
        return None


# ---- the parser ----------------------------------------------------------------------------

BOARD_WORDS = ("board", "boards", "borg", "bored", "bort")
BOARD_LETTERS = ("b", "be", "bee", "s", "es", "ess")          # "S one", "es one", "B2"; S is the Polybench board prefix
RANGE_JOIN = ("to", "through", "thru")
MAX_BOARD = 40
_HOMO_BOARD = {"to": 2, "too": 2, "for": 4, "tree": 3, "won": 1}


def _board_number(toks: list[str], k: int, allow_homophone: bool):
    """(board number 1..MAX_BOARD, next index) at toks[k], or None: "3", "three", "ten", "twenty one", "21".
    Deliberately not _int_words: "1 and 2" must stay two numbers."""
    if k >= len(toks):
        return None
    w = toks[k]
    if allow_homophone and w in _HOMO_BOARD:
        return _HOMO_BOARD[w], k + 1
    if re.fullmatch(r"\d{1,2}", w):
        v = int(w)
        return (v, k + 1) if 1 <= v <= MAX_BOARD else None
    if w in TEENS:
        return TEENS[w], k + 1
    if w in TENS:
        v = TENS[w]
        if k + 1 < len(toks) and toks[k + 1] in DIGIT_WORDS and toks[k + 1] not in HOMOPHONES and DIGIT_WORDS[toks[k + 1]] > 0:
            v += DIGIT_WORDS[toks[k + 1]]
            k += 1
        return (v, k + 1) if v <= MAX_BOARD else None
    if w in DIGIT_WORDS and w not in HOMOPHONES and w not in ("oh", "zero") and DIGIT_WORDS[w] > 0:
        return DIGIT_WORDS[w], k + 1
    return None


def _one_board(toks: list[str], j: int, allow_homophone: bool = False):
    """A single board reference at toks[j] with optional prefix: 's5', 'board 5', 'S 5', 'five'. (n, next) or None."""
    if j >= len(toks):
        return None
    m = re.fullmatch(r"[bs]0*(\d{1,2})", toks[j])
    if m and 1 <= int(m.group(1)) <= MAX_BOARD:
        return int(m.group(1)), j + 1
    if toks[j] in BOARD_WORDS or toks[j] in BOARD_LETTERS:
        j += 1
        if j < len(toks) and toks[j] == "minus" and toks[j - 1] in BOARD_LETTERS:
            j += 1                                  # "S-1" is tokenised as "s minus 1"
        m = re.fullmatch(r"[bs]0*(\d{1,2})", toks[j]) if j < len(toks) else None
        if m and 1 <= int(m.group(1)) <= MAX_BOARD:
            return int(m.group(1)), j + 1           # "board S4"
        return _board_number(toks, j, allow_homophone)
    return _board_number(toks, j, allow_homophone)


_SCOPE_RE = re.compile(r"[\s,;:.]*\b(?:(?:apply(?:ing)?|applies|use|set)\s+(?:it\s+|this\s+|that\s+)?(?:to|for|on)\s+|(?:for|on|to)\s+)?(?:all|every|each)\s+(?:of\s+(?:the\s+)?)?(?:the\s+)?boards?\b[\s,;:.]*", re.I)
_SCOPE_DANGLE = re.compile(r"[\s,;:.]*\b(?:apply(?:ing)?|applies)(?:\s+(?:it|this|that))?(?:\s+(?:to|for|on))?[\s,;:.]*$", re.I)


def strip_board_scope(text: str) -> str:
    """Remove a spoken board-scope phrase ("apply to all boards", "for all board", "on every board", "all boards") from a text value,
    so the batch phrase is not stored as part of the value. A dangling "apply to" left at the end is removed too."""
    t = _SCOPE_RE.sub(" ", text or "")
    t = _SCOPE_DANGLE.sub("", t)
    t = re.sub(r"^[\s,;:.]*(?:apply(?:ing)?|applies)(?:\s+(?:it|this|that))?\s+(?:to|for|on)\b[\s,;:.]*", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" ,;:.")


def _board_at(toks: list[str], i: int):
    """Return (list_of_board_numbers | 'all', next_i) or None.

    Forms: "board three", "boards 1 and 2", "boards one to five", "S1", "S01", "S 1", "es one", "ess ten", "B2",
    "all boards", "every board". Numbers up to MAX_BOARD.
    """
    n = len(toks)
    t = toks[i]
    if t in ("all", "every", "each") and i + 1 < n and toks[i + 1] in ("boards", "board"):
        return "all", i + 2
    if not (re.fullmatch(r"[bs]0*\d{1,2}", t) or t in BOARD_WORDS or t in BOARD_LETTERS):
        return None
    r = _one_board(toks, i, allow_homophone=t in BOARD_WORDS)
    if r is None:
        return None
    boards, k = [r[0]], r[1]
    while k < n:
        if toks[k] in RANGE_JOIN:
            r = _one_board(toks, k + 1)
            if r is not None and r[0] > boards[-1]:
                boards += list(range(boards[-1] + 1, r[0] + 1))
                k = r[1]
                continue
            break
        if toks[k] == "and":
            r = _one_board(toks, k + 1)
            if r is None:
                break
            bare = r[1] == k + 2 and not re.fullmatch(r"[bs]\d+", toks[k + 1])
            if bare and r[1] < n and un.spoken_unit(toks[r[1]:], toks[r[1]:])[0]:
                break                       # "S1 and 2 ohms": the 2 is the value
            if r[0] not in boards:
                boards.append(r[0])
            k = r[1]
            continue
        break
    return boards, k


def parse(text: str, id_index: IdIndex) -> dict:
    """Parse one utterance. Returns a dict with `ok`, `intent` and fields, or `ok: False` and `reason`."""
    toks, orig = _tokens(text)
    res = {"ok": False, "intent": None, "transcript": text, "boards": None, "id": None, "id_candidates": None,
           "value": None, "reason": None}
    if not toks:
        res["reason"] = "empty"
        return res
    # --- pure commands
    words = [t for t in toks if t not in ("uh", "um", "please", "the", "okay", "alright")] or toks
    w0 = words[0]
    if len(words) <= 3:
        if w0 in ACCEPT and (len(words) == 1 or words[1:] == ["all"] or words[1:] == ["a", "o", "i"]
                             or words[1:] == ["aoi"] or words[1:] == ["all", "aoi"]):
            if len(words) == 1:
                return {**res, "ok": True, "intent": "accept"}
            return {**res, "ok": True, "intent": "accept_all_aoi" if "aoi" in words or words[1:] == ["a", "o", "i"] else "accept_all"}
        if len(words) == 1 and w0 in REJECT:
            return {**res, "ok": True, "intent": "reject"}
        if len(words) == 1 and w0 in UNDO:
            return {**res, "ok": True, "intent": "undo"}
        if len(words) == 1 and w0 in SKIP:
            return {**res, "ok": True, "intent": "skip"}
        if len(words) == 1 and w0 in NEXT or words in (["next", "test"], ["next", "row"], ["next", "one"], ["next", "check"]):
            return {**res, "ok": True, "intent": "next"}
        if len(words) == 1 and w0 in BACK:
            return {**res, "ok": True, "intent": "back"}
        if len(words) == 1 and w0 in REPEAT or words in (["say", "again"], ["read", "back"]):
            return {**res, "ok": True, "intent": "repeat"}
        if words in (["scratch", "that"], ["reject", "that"], ["discard", "that"]):
            return {**res, "ok": True, "intent": "reject"}
        if words in (["undo", "that"], ["undo", "last"]):
            return {**res, "ok": True, "intent": "undo"}
    if w0 in SKIP and len(words) > 1:
        from . import rowmode
        sk = rowmode.parse_skip(text)
        if sk:
            return {**res, "ok": True, "intent": "skip_board", "boards": sk[0], "value": {"kind": "text", "text": sk[1]}}
    # --- scan for board / id
    boards = None
    rid = None
    cands = None
    keep: list[int] = []
    i = 0
    n = len(toks)
    row_no = None
    kw_at = next((k for k, t in enumerate(toks) if t in ("note", "text", "defect", "verdict")), n)
    while i < kw_at:
        if toks[i] == "row" and i + 1 < kw_at:
            rn = parse_number(toks, i + 1)
            if rn and rn[0] == rn[0].to_integral_value() and rn[0] > 0:
                row_no = int(rn[0])
                i = rn[1]
                continue
        b = _board_at(toks, i)
        if b is not None:
            if boards is None:
                boards = b[0]
            elif boards != "all" and b[0] != "all":
                boards = list(boards) + [x for x in b[0] if x not in boards]
            i = b[1]
            continue
        m = id_index.match(toks, i) if toks[i] not in ("pass", "fail", "ok", "okay", "note", "text", "defect") else None
        if m is not None and rid is None:
            rid, j, cands = m
            i = j
            continue
        keep.append(i)
        i += 1
    keep += list(range(kw_at, n))
    rest = [toks[k] for k in keep]
    rest_o = [orig[k] for k in keep]
    res["boards"] = boards
    res["id"] = rid
    res["row_no"] = row_no
    res["id_candidates"] = cands if cands and len(cands) > 1 else None
    # --- strip fillers (not inside text)
    # pass / fail
    if rest and rest[0] in ("pass", "passed", "fail", "failed"):
        kind = "pass" if rest[0].startswith("pass") else "fail"
        if len(rest) > 1:
            res["reason"] = "extra words after " + kind
            return res
        return {**res, "ok": True, "intent": kind}
    # note / text / defect
    if "verdict" in rest:
        k = rest.index("verdict")
        body = " ".join(w for w in rest_o[k + 1:]).strip()
        m_raw = re.search(r"\bverdict\b[\s,:]*(.*)$", text, re.I | re.S)
        body = (m_raw.group(1) if m_raw else body).strip().rstrip(".").strip()
        if not body:
            res["reason"] = "nothing after verdict"
            return res
        return {**res, "ok": True, "intent": "value", "value": {"kind": "verdict", "text": body.upper()}}
    for kw in ("note", "text", "defect"):
        if kw in rest:
            k = rest.index(kw)
            if k > 0 and any(w not in FILLERS for w in rest[:k]):
                res["reason"] = "words before " + kw
                return res
            m_raw = re.search(r"\b%s\b[\s,:]*(.*)$" % kw, text, re.I | re.S)
            body = (m_raw.group(1) if m_raw else " ".join(rest_o[k + 1:])).strip().rstrip(".").strip()
            if not body:
                res["reason"] = "nothing after " + kw
                return res
            if kw == "note":
                return {**res, "ok": True, "intent": "note", "value": {"kind": "text", "text": body}}
            body = strip_board_scope(body) or body
            txt = ("defect: " + body) if kw == "defect" else body
            return {**res, "ok": True, "intent": "value", "value": {"kind": "text", "text": txt}}
    rest_nf = [w for w in rest if w not in FILLERS or w in ("for", "to")]
    # re-add fillers that are number homophones when they are part of numbers: use rest (with fillers) for numbers
    if not rest_nf and not rest:
        if rid or boards or row_no:
            if (rid or row_no) and not boards:
                return {**res, "ok": True, "intent": "goto"}
            if boards and not rid:
                return {**res, "ok": True, "intent": "set_board"}
            res["reason"] = "no value"
            return res
        res["reason"] = "empty"
        return res
    # value
    v = _parse_value(rest, rest_o)
    if v is None:
        res["reason"] = "value not understood: " + " ".join(rest)
        return res
    if isinstance(v, str):
        res["reason"] = v
        return res
    return {**res, "ok": True, "intent": "value", "value": v}


def _parse_value(rest: list[str], rest_o: list[str]):
    toks = [t for t in rest if t not in ("uh", "um", "please", "the", "is", "reads", "equals", "equal", "at", "its", "value", "reading", "measured", "got", "now", "so", "of", "a")]
    orig = [o for t, o in zip(rest, rest_o) if t in toks]
    # drop leading/trailing 'and'
    if not toks:
        return None
    # --- hex
    if toks[0].startswith("0x") and len(toks) == 1 and re.fullmatch(r"0x[0-9a-f]+", toks[0]):
        return {"kind": "hex", "text": "0x" + toks[0][2:].upper()}
    if toks[0] == "hex" or (len(toks) > 1 and toks[0] in ("zero", "0") and toks[1] == "x"):
        body = toks[1:] if toks[0] == "hex" else toks[2:]
        s = ""
        for t in body:
            if t in DIGIT_WORDS and t not in HOMOPHONES:
                s += str(DIGIT_WORDS[t])
            elif re.fullmatch(r"[0-9a-f]+", t):
                s += t.upper()
            elif t in LETTER_NAMES and LETTER_NAMES[t] in "abcdef":
                s += LETTER_NAMES[t].upper()
            elif t in TENS or t in TEENS:
                s += str((TENS | TEENS)[t])
            else:
                return "hex digits not understood: " + t
        return {"kind": "hex", "text": "0x" + s} if s else None
    # --- OL
    if toks in (["o", "l"], ["over", "range"], ["out", "of", "range"], ["over", "load"], ["open", "loop"], ["over", "limit"]) or \
            (len(toks) == 1 and toks[0] in OL_WORDS):
        return {"kind": "ol"}
    if len(toks) == 1 and toks[0] in ("absent", "noack", "manual"):
        return {"kind": "verdict", "text": toks[0].upper()}
    # --- ok / yes / no
    if all(t in OK_WORDS for t in toks):
        return {"kind": "ok"}
    if all(t in YES | NO for t in toks):
        return {"kind": "yn", "values": ["Y" if t in YES else "N" for t in toks]}
    # --- number + unit
    num = parse_number(toks, 0)
    if num is None:
        return None
    val, j = num
    unit = None
    prefix = None
    if j < len(toks):
        u, used = un.spoken_unit(toks[j:], orig[j:])
        if u:
            unit, j = u, j + used
        elif toks[j] in ("k", "kilo", "mega", "meg", "milli", "micro") or (len(toks[j]) == 1 and toks[j] in "kKmM"):
            o = orig[j]
            prefix = {"k": "k", "kilo": "k", "mega": "M", "meg": "M", "milli": "m", "micro": "µ"}.get(toks[j])
            if prefix is None:
                prefix = "M" if o == "M" else ("m" if o == "m" else "k")
            j += 1
            # optional trailing base word: handled by spoken_unit above if present
    if j < len(toks):
        return "extra words after the number: " + " ".join(toks[j:])
    return {"kind": "number", "number": str(val), "unit": unit, "prefix": prefix}
