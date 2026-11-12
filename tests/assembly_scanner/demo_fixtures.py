"""DEMO fixtures of the Assembly Scanner tests, generated in code (no binary files, no client data).

* Two purchasing BOM workbooks with the layouts the BOM importer has to cope with (a basket with extras and a reference
  sheet; a BOM with an Order sheet, DNF rows and formulas). Every part number is synthetic (`9xx-DEMO-...`).
* Bag labels: a Code 128 / Data Matrix label rendered with PIL (portrait, saved as HEIC) and a tilted live-camera frame of
  another label (JPEG). The barcode symbols are written by zxing-cpp's own writer; nothing here is a photograph.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- part numbers (synthetic)
PN_R2K2 = "901-DEMO-R2K2"          # R4, R5
PN_C100N = "902-DEMO-C100N"        # nine decoupling capacitors
PN_XCVR = "903-DEMO-XCVR/MF"       # IC2, a '/' in the part number
PN_ADC = "904-DEMO-ADC1"           # IC1
PN_R1K = "905-DEMO-R1K"            # R8, R11, R12, R13
PN_C1N2 = "906-DEMO-C1N2"          # C14, C16 (a decided row below the original block)
PN_MCU = "907-DEMO-MCU1"           # IC3
PN_TRIM = "920-DEMO-R560"          # tuning kit, no placement
PN_PASTE = "930-DEMO-PASTE"        # consumable, no placement
PN_NOT_IN_BOM = "999-DEMO-NOTINBOM"
PN_C22U = "940-DEMO-C22U"          # C5, C22 on the second BOM (BOM + Order sheets)
PN_CONN = "950-DEMO-CONN"          # J2, DNF on the second BOM

BASKET_NAME = "demo_basket.xlsx"
ORDER_NAME = "demo_bom_order.xlsx"
LABEL_A_NAME = "label_a.heic"
LABEL_B_NAME = "label_b_tilted.jpg"

# (designators, qty, PN) in the Basket's row order: Basket rows 5..28, the last three below the "decided" line
_BASKET = [
    ("C1", 1, "910-DEMO-C4U7"), ("C2,C3,C4,C7,C11,C13,C18,C19,C20", 9, PN_C100N), ("C5,C12,C22", 3, "911-DEMO-C10U"),
    ("C6,C10,C21", 3, "912-DEMO-C1U"), ("C8,C9", 2, "913-DEMO-C8P"), ("C17", 1, "914-DEMO-C22N"), ("D1", 1, "915-DEMO-LED"),
    ("D2", 1, "916-DEMO-TVS"), ("IC1", 1, PN_ADC), ("IC2", 1, PN_XCVR), ("IC4", 1, "917-DEMO-BUCK"), ("L1", 1, "918-DEMO-L10U"),
    ("R2,R15", 2, "919-DEMO-R10K"), ("R3", 1, "921-DEMO-R10K6"), ("R4,R5", 2, PN_R2K2), ("R6", 1, "922-DEMO-R620"),
    ("R7", 1, "923-DEMO-R4R7"), ("R8,R11,R12,R13", 4, PN_R1K), ("R9,R10", 2, "924-DEMO-R43"), ("R14", 1, "925-DEMO-NTC10K"),
    ("Y1", 1, "926-DEMO-XTAL"), ("IC3", 1, PN_MCU), ("C14,C16", 2, PN_C1N2), ("C15", 1, "927-DEMO-C12N"),
]
_TUNING = [(f"Load capacitor option {i}", f"930-DEMO-CL{i}") for i in range(1, 6)] \
    + [(f"Filter bracket option {i}", f"931-DEMO-CF{i}") for i in range(1, 5)] \
    + [("Trim resistor 560", PN_TRIM), ("Trim resistor 680", "932-DEMO-R680"),
       ("Backup capacitor 1n", "933-DEMO-C1N"), ("Backup capacitor 10n", "934-DEMO-C10N")]
_CONSUMABLES = [("Solder paste", PN_PASTE), ("Tacky flux", "930-DEMO-FLUX"), ("Foam-tip swabs", "930-DEMO-SWAB")]


def build_basket_bom(path: Path) -> Path:
    """A basket workbook: Basket (placement), Tuning kit and Consumables (extras), Parts mapping (reference)."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("Parts mapping")
    ws.append(["DEMO-1 parts mapping"])
    ws.append(["Reference only: sourcing candidates, nothing is ordered from this sheet."])
    ws.append(["Value", "Package", "Spec source", "Manufacturer", "Mfr P/N", "Mouser P/N", "Unit price"])
    for des, _q, pn in _BASKET:
        ws.append([f"value of {pn}", f"{des} pkg", "demo", "DEMO Corp", pn.split("-", 1)[1], pn, 0.1])

    ws = wb.create_sheet("Basket")
    ws.append(["DEMO-1 basket, 10 boards"])
    ws.append(["Spare rule: invented for the demo."])
    ws.append([])
    ws.append(["Designators", "Qty/board (export)", "Spare rule", "Qty ordered", "Mouser P/N", "Unit price", "Extended price", "Status"])
    for des, q, pn in _BASKET:
        ws.append([des, q, "demo", q * 10, pn, 0.1, q, "RESOLVED"])
    ws.append([None, None, None, None, None, "RESOLVED LINES TOTAL", 42.0, None])             # row 29: a totals row
    ws.append([])
    ws.append(["All three decided lines are demo text on a prose row."])                       # row 31: prose

    ws = wb.create_sheet("Tuning kit")
    ws.append(["Tuning and experiment parts"])
    ws.append(["Every line is justified in the demo."])
    ws.append([])
    ws.append(["Item", "Justification", "Qty", "Mfr P/N", "Mouser P/N", "Unit price", "Extended", "Stock"])
    for item, pn in _TUNING:
        ws.append([item, "demo", 10, pn.split("-", 1)[1], pn, 0.02, 0.2, 1000])
    ws.append([None, None, None, None, None, None, "TUNING KIT TOTAL", 3.0])

    ws = wb.create_sheet("Consumables")
    ws.append(["Assembly consumables"])
    ws.append([])
    ws.append(["Item", "Spec / reasoning", "Qty", "Mfr P/N", "Mouser P/N", "Unit price", "Extended", "Stock"])
    for item, pn in _CONSUMABLES:
        ws.append([item, "demo", 1, pn.split("-", 1)[1], pn, 10.0, 10.0, 100])
    ws.append(["Stencil, laser-cut", "demo", 1, "-", "N/A - not a distributor part", None, None, None])   # row 7
    ws.append([None, None, None, None, None, "Distributor-line subtotal", 30.0, None])
    wb.save(path)
    return path


_BOM2 = [  # refdes, qty, value, package, mpn, mfr, pn, description, fitted
    ("IC1", 1, "DEMO-MCU", "QFN56", "DEMO-MCU", "DEMO Corp", "956-DEMO-MCU", "SoC", "Fitted"),
    ("U1", 1, "DEMO-ACCEL", "LGA-16", "DEMO-ACCEL", "DEMO Corp", "957-DEMO-ACCEL", "Accelerometer", "Fitted"),
    ("IC2", 1, "DEMO-XCVR", "VSON-8", "DEMO-XCVR", "DEMO Corp", "958-DEMO-XCVR", "Transceiver", "Fitted"),
    ("C2", 1, "100nF 50V X7R", "0402", "DEMO-C100N", "DEMO Corp", "959-DEMO-C100N", "Capacitor", "Fitted"),
    ("C5, C22", 2, "22uF 25V X5R", "0805", "DEMO-C22U", "DEMO Corp", PN_C22U, "Capacitor", "Fitted"),
    ("C3, C4, C7, C8, C9", 5, "100nF 16V X7R", "0402", "DEMO-C100N16", "DEMO Corp", "960-DEMO-C100N16", "Capacitor", "Fitted"),
    ("R3", 1, "22R 1%", "0402", "DEMO-R22", "DEMO Corp", "961-DEMO-R22", "Resistor", "Fitted"),
    ("R4", 1, "10k 1%", "0402", "DEMO-R10K", "DEMO Corp", "962-DEMO-R10K", "Resistor", "Fitted"),
    ("R5, R7", 2, "5.1k 1%", "0402", "DEMO-R5K1", "DEMO Corp", "963-DEMO-R5K1", "Resistor", "Fitted"),
    ("R8", 1, "0R link", "0402", "DEMO-R0", "DEMO Corp", "964-DEMO-R0", "Resistor", "Fitted"),
]
_BOM2_DNF = [("R1", "DNF - termination footprint", "0603"), ("R2", "DNF - mode resistor", "0402"), ("R9", "DNF - series link", "0402"),
             ("C23", "DNF - tuning capacitor", "0402"), ("C24", "DNF - tuning capacitor", "0402")]


def build_order_bom(path: Path) -> Path:
    """A BOM workbook with an Order sheet (formulas, a 'Customer # (cart)' column), DNF rows, a kit sheet and a cost sheet."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("BOM")
    ws.append(["Refdes", "Qty/board", "Value", "Package", "MPN", "Manufacturer", "Mouser P/N", "Description", "Fitted/DNF", "Side",
               "Unit price GBP", "Extended per board (GBP)", "Notes"])
    for r in _BOM2:
        ws.append([*r[:8], r[8], "Top", 0.1, 0.1 * r[1], "demo"])
    for ref, val, pkg in _BOM2_DNF:                                                      # rows 12..16 of this small BOM
        ws.append([ref, 1, val, pkg, "-", "-", "-", "-", "DNF", "Top", 0, 0, "kept as a footprint"])
    ws.append(["J2", 1, "DNF - RF connector", "SMD", "DEMO-CONN", "DEMO Corp", PN_CONN, "Connector", "DNF", "Top", 0, 0, "not fitted"])
    n = ws.max_row
    ws.append(["TOTAL (fitted only)"] + [None] * 10 + [f"=SUM(L2:L{n})", None])

    ws = wb.create_sheet("Order")
    ws.append(["Mouser P/N", "Manufacturer P/N", "Description", "Qty ordered", "Unit price GBP", "Line total GBP", "Purpose",
               "Stock", "Lead time", "Substitution / notes", "Customer # (cart)"])
    rows = [(r[6], r[4], r[7], 6, 0.1, "Assembly", r[0]) for r in _BOM2] + [(PN_CONN, "DEMO-CONN", "Connector", 5, 0.26, "Kit", "J2")]
    for i, (pn, mpn, desc, q, price, purpose, cust) in enumerate(rows, 2):
        ws.append([pn, mpn, desc, q, price, f"=D{i}*E{i}", purpose, "100 In Stock", "10 Days", "demo", cust])
    ws.append(["TOTAL", None, None, None, None, f"=SUM(F2:F{len(rows) + 1})"])

    ws = wb.create_sheet("Cost per PCB")
    ws.append(["Per-board cost"])
    ws.append(["Inputs"])
    ws.append(["Bare PCB cost", 4.5, "demo"])
    ws.append(["Components per board", "=BOM!L18", None])

    ws = wb.create_sheet("Kit and Jig")
    ws.append(["Tuning kit (DNF parts)"])
    ws.append(["Mouser P/N", "Manufacturer P/N", "Value", "Qty", "Unit GBP", "Line total GBP", "Notes"])
    for i in range(1, 4):
        ws.append([f"965-DEMO-K{i}", f"DEMO-K{i}", f"Kit value {i}", 10, 0.05, 0.5, "demo"])
    ws.append(["KIT TOTAL", None, None, None, None, 1.5, None])

    ws = wb.create_sheet("Notes - Sources")
    ws.append(["Item", "Detail"])
    ws.append(["Scope", "Synthetic workbook for the scanner tests."])
    wb.save(path)
    return path


# ---------------------------------------------------------------- labels
GS, RS, EOT = "\x1d", "\x1e", "\x04"      # the ISO/IEC 15434 separators a Mouser Data Matrix carries


def label_texts(mouser_pn: str, cust_pn: str, *, po="40000001", line="018", qty="30", coo="TW", invoice="090000001",
                mfr="DEMO Corp", route="57DEMO"):
    """(data matrix text, [code 128 texts]) of a distributor bag label; same field order as the decoder returns them."""
    dm = f"[)>{RS}06{GS}K{po}{GS}14K{line}{GS}P{cust_pn}{GS}Q{qty}{GS}11K{invoice}{GS}4L{coo}{GS}1V{mfr}{RS}{EOT}"
    return dm, [po, line, mouser_pn, cust_pn, qty, coo, invoice, route]


def _barcode(fmt: str, text: str, scale: int):
    """The barcode image (PIL, mode L) written by zxing-cpp."""
    import numpy as np
    import zxingcpp
    from PIL import Image
    f = getattr(zxingcpp.BarcodeFormat, fmt)
    if hasattr(zxingcpp, "create_barcode"):                       # zxing-cpp 3.x
        img = zxingcpp.create_barcode(text, f).to_image(scale=scale, add_quiet_zones=True)
    else:                                                         # zxing-cpp 2.x
        img = zxingcpp.write_barcode(f, text, 0, 0, 10)
    a = np.array(img)
    return Image.fromarray(a if a.ndim == 2 else a[..., 0])


def render_label(mouser_pn: str, cust_pn: str, size=(1440, 1920), scale: int = 3, **kw):
    """A portrait bag-label image: a Data Matrix and eight Code 128 symbols with their captions, on a grey table."""
    from PIL import Image, ImageDraw
    dm, codes = label_texts(mouser_pn, cust_pn, **kw)
    w, h = size
    im = Image.new("L", (w, h), 150)
    d = ImageDraw.Draw(im)
    d.rectangle([w // 12, h // 16, w - w // 12, h - h // 16], fill=255)
    dmi = _barcode("DataMatrix", dm, 9)
    im.paste(dmi, (w - w // 12 - dmi.width - 20, h // 16 + 20))
    caps = ["PO", "LINE", "MOUSER P/N", "CUST P/N", "QTY", "ORIGIN", "INVOICE", "ROUTE"]
    y = h // 16 + dmi.height + 60
    for cap, text in zip(caps, codes):
        d.text((w // 12 + 30, y), f"{cap}: {text}", fill=0)
        bc = _barcode("Code128", text, scale)
        im.paste(bc, (w // 12 + 30, y + 14))
        y += bc.height + 54
    return im.convert("RGB")


def render_tilted_frame(mouser_pn: str, cust_pn: str, angle: float = 5.0, size=(1920, 1080)):
    """A live-camera-like frame (1080p landscape): the label small and tilted by `angle` degrees, with a little noise."""
    import numpy as np
    from PIL import Image
    label = render_label(mouser_pn, cust_pn, size=(900, 1500), scale=2)
    label = label.rotate(angle, expand=True, fillcolor=(120, 120, 120), resample=Image.BICUBIC)
    w, h = size
    frame = Image.new("RGB", size, (110, 110, 115))
    frame.paste(label.resize((int(label.width * 0.72), int(label.height * 0.72)), Image.LANCZOS), (w // 2 - 330, -40))
    a = np.array(frame).astype(np.int16)
    rng = np.random.default_rng(7)
    a += rng.integers(-6, 7, a.shape[:2])[..., None]
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def build_labels(folder: Path) -> dict[str, Path]:
    """Writes the two label images; returns {name: path}. Label A is a HEIC (the phone-photo path), B a tilted JPEG."""
    import pillow_heif
    pillow_heif.register_heif_opener()
    folder.mkdir(parents=True, exist_ok=True)
    a, b = folder / LABEL_A_NAME, folder / LABEL_B_NAME
    render_label(PN_R2K2, "DEMO1 R4,5").save(a, quality=92)
    render_tilted_frame(PN_XCVR, "DEMO1 IC2").save(b, quality=92)
    return {LABEL_A_NAME: a, LABEL_B_NAME: b}
