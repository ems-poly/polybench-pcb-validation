"""Package-name table for the hand-assembly order: nominal body size and height class of common SMD packages.

Data only. `partsort` falls back to this table when neither the BOM line (`height_mm`, `body_mm`) nor the ECAD footprint says more.
Sizes are nominal (length, width, height) in mm, good enough to order 0402 before 0603 before 0805; they are not a drawing.
"""
from __future__ import annotations

# imperial chip codes: (length, width, height)
CHIP = {
    "01005": (0.4, 0.2, 0.13), "0201": (0.6, 0.3, 0.3), "0402": (1.0, 0.5, 0.5), "0603": (1.6, 0.8, 0.8), "0805": (2.0, 1.25, 1.0),
    "1206": (3.2, 1.6, 1.1), "1210": (3.2, 2.5, 1.3), "1812": (4.5, 3.2, 1.5), "2010": (5.0, 2.5, 0.6), "2512": (6.3, 3.2, 0.6),
}

# (regex on the upper-cased package text, class, nominal (l, w, h) or None, label). First match wins, so specific entries come first.
# classes: 1 low two-terminal, 2 SOT and small ICs, 3 QFN/TSSOP/SOIC ICs, 4 crystals / inductors / tall, 5 connectors / through-hole
PATTERNS = [
    (r"SOD[-_ ]?(923|723)", 1, (0.8, 0.6, 0.4), "SOD-923"),
    (r"SOD[-_ ]?(523|323)", 1, (1.7, 1.25, 0.9), "SOD-323"),
    (r"SOD[-_ ]?(123|128)", 1, (2.7, 1.6, 1.1), "SOD-123"),
    (r"\bSMA\b|DO[-_ ]?214AC", 1, (4.3, 2.6, 2.3), "SMA"),
    (r"\bSMB\b|DO[-_ ]?214AA", 1, (4.6, 3.6, 2.3), "SMB"),
    (r"\bSMC\b|DO[-_ ]?214AB", 1, (7.0, 6.0, 2.3), "SMC"),
    (r"SOT[-_ ]?(23|323|363|523|563|666|723|883)|SC[-_ ]?70|SC[-_ ]?88", 2, (2.9, 1.3, 1.1), "SOT-23 / SC-70"),
    (r"SOT[-_ ]?(89|223|143|353)", 3, (4.5, 2.5, 1.6), "SOT-89 / SOT-223"),
    (r"X2SON|USON|UDFN|XSON", 2, (1.5, 1.5, 0.5), "small SON/UDFN"),
    (r"VQFN|WQFN|UQFN|QFN|VSON|WSON|\bDFN|HVSON|LGA|MLP|MLF", 3, None, "QFN/DFN"),
    (r"TSSOP|SSOP|MSOP|VSSOP|TVSOP|HTSSOP", 3, None, "TSSOP/MSOP"),
    (r"SOIC|\bSOP\b|\bSO[-_ ]?\d", 3, None, "SOIC"),
    (r"TQFP|LQFP|QFP|PLCC|BGA|WLCSP", 3, None, "QFP/BGA"),
    (r"NX\d{4}|(?<!\d)(2016|2520|3225|5032|7050)(?!\d)|\bHC[-_ ]?49|\bXTAL|CRYSTAL|OSCILL", 4, None, "crystal / oscillator"),
    (r"WE[-_ ]?MAPI[-_ ]?3015", 4, (3.0, 3.0, 1.5), "inductor WE-MAPI 3015"),
    (r"WE[-_ ]?MAPI|\bMAPI|INDUCT|CHOKE|\bSRR|\bSRN|FERRITE|\bXAL|\bXFL|\bIHLP|\bLPS|\bCDRH", 4, None, "inductor"),
    (r"ELECTROLYTIC|\bCAP[-_ ]?AL|\bRADIAL|\bTANT", 4, None, "tall capacitor"),
    (r"\bTO[-_ ]?(220|252|263|92|247)|\bDPAK|\bD2PAK", 4, None, "power package"),
    (r"CONN|HEADER|SOCKET|TERMINAL|JST|MOLEX|USB|RJ45|\bPIN[-_ ]?\d|\bTHT\b|THROUGH|DIP[-_ ]?\d|\bPLUG|\bJACK|SCREW|D-?SUB|\bFFC|\bFPC", 5, None, "connector / through-hole"),
]

# designator prefix -> kind
KINDS = {
    "R": "passive", "C": "passive", "FB": "passive", "RN": "passive", "RT": "passive", "NTC": "passive", "F": "passive",
    "D": "diode", "LED": "diode", "TVS": "diode", "ZD": "diode", "DZ": "diode",
    "Q": "ic", "U": "ic", "IC": "ic",
    "L": "inductor", "Y": "xtal", "X": "xtal",
    "J": "conn", "P": "conn", "CN": "conn", "K": "conn", "SW": "conn", "H": "conn", "CON": "conn", "BT": "conn", "TB": "conn",
}
# kind -> (class, text) used when nothing better is known
KIND_CLASS = {"passive": (0, "R/C designator"), "diode": (1, "D designator"), "ic": (3, "IC designator"),
              "inductor": (4, "L designator"), "xtal": (4, "Y designator"), "conn": (5, "J designator")}
