"""Name resolution and selection sets, shared by the bridge (step 1, plain highlight) and the scanner client (test highlight,
Assembly aid). Pure functions over a BoardSnapshot: they decide what to select and what is missing; they never talk to KiCad.

Rules (requirements): exact match on KiCad's reference, case-insensitive (KC-13); a name that is not a reference but is a net
selects every pad of the net (KC-15); `REF.PIN` selects pad PIN of footprint REF (KC-17); an optional alias table maps a plan or
BOM name to a board reference or net name before matching and is the only mapping there is (KC-44, KC-45): a name is never
guessed, and the Eagle importer's trailing 0 is never stripped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import BoardSnapshot, FootprintInfo, PadInfo


@dataclass
class Plan:
    """What to select, in KiCad ids, and what could not be resolved."""
    ids: list[str] = field(default_factory=list)                 # KIIDs, in selection order, no duplicates
    labels: dict[str, str] = field(default_factory=dict)         # KIID -> "R9" or "IC1.10"
    parts: list[str] = field(default_factory=list)               # found footprints, KiCad spelling
    parts_missing: list[str] = field(default_factory=list)
    pins: list[str] = field(default_factory=list)                # found, as requested ("IC1.10")
    pins_missing: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)                # found, KiCad spelling
    nets_missing: list[str] = field(default_factory=list)
    aliased: dict[str, str] = field(default_factory=dict)        # requested name -> board name that was used
    net_pad_count: dict[str, int] = field(default_factory=dict)
    parent: dict[str, str] = field(default_factory=dict)          # pad KIID -> KIID of its footprint
    covered: list[str] = field(default_factory=list)              # pads left out because their footprint is selected (KiCad would drop them)

    def add(self, kiid: str, label: str, parent: str = "") -> None:
        if kiid and kiid not in self.labels:
            self.ids.append(kiid)
            self.labels[kiid] = label
            if parent:
                self.parent[kiid] = parent

    def finalize(self) -> "Plan":
        """KiCad's selection holds a footprint OR its pads: a pad of a selected footprint is dropped by KiCad (measured live,
        17 Oct 2026: R9 and R9.2 requested, read-back R9 only). Leave such pads out of the request, and say so."""
        fps = {i for i in self.ids if i not in self.parent}
        keep = [i for i in self.ids if self.parent.get(i) not in fps]
        self.covered = [self.labels[i] for i in self.ids if i not in keep]
        self.ids = keep
        return self

    @property
    def alias_note(self) -> str:
        return ", ".join(f"{a}→{b}" for a, b in self.aliased.items())


def norm_aliases(table) -> dict[str, str]:
    """{requested name (upper-case): board name}. Anything that is not a str->str mapping is ignored."""
    out: dict[str, str] = {}
    if isinstance(table, dict):
        for k, v in table.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                out[k.strip().upper()] = v.strip()
    return out


def clean(names) -> list[str]:
    seen, out = set(), []
    for n in names or ():
        n = str(n).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


class Resolver:
    def __init__(self, snap: BoardSnapshot, aliases: dict[str, str] | None = None):
        self.snap = snap
        self.aliases = norm_aliases(aliases) if aliases else {}
        self.refs = snap.by_ref()
        self._nets = {n.upper(): n for n in snap.nets}
        for f in snap.footprints:                      # a net named on a pad counts even if the net list was not read
            for p in f.pads:
                if p.net:
                    self._nets.setdefault(p.net.upper(), p.net)
        self._net_pads = snap.net_pads()

    # -- single names
    def _alias(self, name: str, plan: Plan, exists=None) -> str:
        """The board name to look for. An exact match of the kind asked for wins: an alias is only the fallback for a name that is
        not on the board (found live: the plan's net '5V' is a real net while its test pad 'TP2' is aliased from '5V')."""
        if exists is not None and exists(name):
            return name
        to = self.aliases.get(name.upper())
        if to and to.upper() != name.upper():
            plan.aliased[name] = to
            return to
        return name

    def footprint(self, name: str) -> FootprintInfo | None:
        return self.refs.get(name.upper())

    def net(self, name: str) -> str | None:
        return self._nets.get(name.upper())

    def pad(self, fp: FootprintInfo, number: str) -> list[PadInfo]:
        return [p for p in fp.pads if p.number.upper() == number.upper()]

    # -- the three kinds of request
    def part(self, name: str, plan: Plan, allow_net: bool) -> None:
        target = self._alias(name, plan, lambda n: self.footprint(n) is not None or (allow_net and self.net(n) is not None))
        fp = self.footprint(target)
        if fp is not None:
            plan.add(fp.kiid, fp.ref)
            if fp.ref not in plan.parts:
                plan.parts.append(fp.ref)
            return
        if allow_net:
            net = self.net(target)
            if net is not None:
                self._add_net(net, plan)
                return
        plan.parts_missing.append(name)
        plan.aliased.pop(name, None)

    def pin(self, spec: str, plan: Plan) -> None:
        ref, _, num = spec.partition(".")
        target = self._alias(ref, plan, lambda n: self.footprint(n) is not None)
        fp = self.footprint(target)
        pads = self.pad(fp, num) if (fp is not None and num) else []
        if not pads:
            plan.pins_missing.append(spec)
            if fp is None:
                plan.aliased.pop(ref, None)
            return
        for p in pads:
            plan.add(p.kiid, f"{fp.ref}.{p.number}", fp.kiid)
        plan.pins.append(spec)

    def _add_net(self, net: str, plan: Plan) -> None:
        pads = self._net_pads.get(net, [])
        for f, p in pads:
            plan.add(p.kiid, f"{f.ref}.{p.number}", f.kiid)
        if net not in plan.nets:
            plan.nets.append(net)
        plan.net_pad_count[net] = len(pads)

    def net_request(self, name: str, plan: Plan) -> None:
        target = self._alias(name, plan, lambda n: self.net(n) is not None)
        net = self.net(target)
        if net is None:
            plan.nets_missing.append(name)
            plan.aliased.pop(name, None)
            return
        self._add_net(net, plan)


def plan_designators(snap: BoardSnapshot, designators, aliases=None) -> Plan:
    """The bridge's plain highlight: references, and nets for names that are not references (KC-13..KC-15)."""
    plan = Plan()
    r = Resolver(snap, aliases)
    for n in clean(designators):
        r.part(n, plan, allow_net=True)
    return plan.finalize()


def plan_targets(snap: BoardSnapshot, parts, pins=(), nets=(), aliases=None) -> Plan:
    """The test highlight (KC-17, KC-18): footprints of `parts`, pads `REF.PIN`, every pad of each net in `nets`."""
    plan = Plan()
    r = Resolver(snap, aliases)
    for n in clean(parts):
        r.part(n, plan, allow_net=False)
    for p in clean(pins):
        r.pin(p, plan)
    for n in clean(nets):
        r.net_request(n, plan)
    return plan.finalize()
