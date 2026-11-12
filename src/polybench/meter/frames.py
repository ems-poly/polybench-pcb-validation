"""Frame layer for the UNI-T UT61+/UT161 family meters behind a CH9329 HID-to-UART cable.

Facts (docs/MULTIMETER.md, section "Sources"): written from the Polybench brief's fact sheet, the WCH CH9329 datasheet and our own
captures of a UT161E. No third-party driver code was read or copied.

Frame:    AB CD <len> <payload...> <sum_hi> <sum_lo>
          len = number of bytes after it (payload plus the two checksum bytes)
          sum = 16-bit sum of every byte before the checksum, high byte first
Commands: a one-byte payload (Get Name 0x5F, Get Measurement 0x5E), so the checksum is 0x01DA / 0x01D9. ONLY these two are ever built here.
Measurement payload (14 bytes, len 0x10): mode, range (0x30 | index), 7 display characters, 2 bar-graph bytes, 3 flag bytes (0x30 | bits).
CH9329 report: input = <len> <len UART bytes>, a leading 0x00 report ID is tolerated; output = 0x00 <len> <UART bytes> padded to 65.

Readiness: HOST-ONLY (fake device). Layout checked against captured UT161E frames (HARDWARE-VALIDATED for those captures only).
"""
from __future__ import annotations

from dataclasses import dataclass

HEADER = b"\xab\xcd"
CMD_GET_NAME = 0x5F
CMD_GET_MEASUREMENT = 0x5E
ALLOWED_COMMANDS = (CMD_GET_NAME, CMD_GET_MEASUREMENT)      # the others press the meter's buttons: never built
MEAS_LEN = 0x10
MEAS_TOTAL = 19
MAX_LEN = 0x40                                              # larger length bytes are noise, not frames
OUT_REPORT = 65


class FrameError(ValueError):
    pass


def checksum(data: bytes) -> int:
    return sum(data) & 0xFFFF


def build_frame(payload: bytes) -> bytes:
    body = HEADER + bytes([len(payload) + 2]) + bytes(payload)
    c = checksum(body)
    return body + bytes([c >> 8, c & 0xFF])


def command_frame(cmd: int) -> bytes:
    """Get Name `AB CD 03 5F 01 DA`, Get Measurement `AB CD 03 5E 01 D9`. Any other command byte is refused."""
    if cmd not in ALLOWED_COMMANDS:
        raise FrameError(f"command 0x{cmd:02x} is not allowed: only Get Name and Get Measurement are ever sent")
    return build_frame(bytes([cmd]))


def out_report(uart_bytes: bytes) -> bytes:
    """One CH9329 output report as hidapi wants it: report ID 0, byte count, the UART bytes, zero padding (65 bytes)."""
    if len(uart_bytes) > 63:
        raise FrameError("too many bytes for one report")
    return (b"\x00" + bytes([len(uart_bytes)]) + bytes(uart_bytes)).ljust(OUT_REPORT, b"\x00")


def in_report_payload(report: bytes) -> bytes:
    """UART bytes carried by one input report: `<len> <bytes>`; a leading 0x00 report ID (hidapi on some platforms) is stripped."""
    r = bytes(report)
    if len(r) > 1 and r[0] == 0:
        r = r[1:]
    if not r:
        return b""
    n = r[0]
    return r[1:1 + n]


class FrameBuffer:
    """Reassembles frames from UART bytes that arrive in pieces and resynchronises on the header.
    `bad` counts frames dropped for a wrong checksum, `junk` counts bytes skipped while looking for a header."""

    def __init__(self):
        self.buf = bytearray()
        self.bad = 0
        self.junk = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            i = self.buf.find(HEADER)
            if i < 0:
                keep = 1 if self.buf[-1:] == b"\xab" else 0     # a header may be split between two reports
                self.junk += len(self.buf) - keep
                del self.buf[:len(self.buf) - keep]
                return out
            if i:
                self.junk += i
                del self.buf[:i]
            if len(self.buf) < 3:
                return out
            n = self.buf[2]
            if n < 2 or n > MAX_LEN:
                self.junk += 1
                del self.buf[:1]
                continue
            total = 3 + n
            if len(self.buf) < total:
                return out
            frame = bytes(self.buf[:total])
            c = checksum(frame[:-2])
            if frame[-2] == c >> 8 and frame[-1] == c & 0xFF:
                out.append(frame)
                del self.buf[:total]
            else:
                self.bad += 1
                del self.buf[:2]                                # skip this header and look again


def payload_of(frame: bytes) -> bytes:
    return frame[3:-2]


@dataclass(frozen=True)
class Flags:
    rel: bool = False
    hold: bool = False
    min: bool = False
    max: bool = False
    hv: bool = False
    low_batt: bool = False
    manual_range: bool = False
    auto_off: bool = False
    neg_bar: bool = False
    pmin: bool = False
    pmax: bool = False
    ac: bool = False

    def active(self) -> list[str]:
        names = [("REL", self.rel), ("HOLD", self.hold), ("MIN", self.min), ("MAX", self.max), ("P-MIN", self.pmin), ("P-MAX", self.pmax)]
        return [n for n, on in names if on]

    def display(self) -> list[str]:
        """Flags worth showing on the card."""
        out = self.active()
        if self.low_batt:
            out.append("LOW BATTERY")
        if self.hv:
            out.append("HV")
        return out


@dataclass(frozen=True)
class Measurement:
    mode: int
    range_idx: int
    display: str            # 7 characters as sent
    flags: Flags
    flag_bytes: tuple[int, int, int]
    raw: bytes              # the whole frame

    @property
    def hex(self) -> str:
        return self.raw.hex()


def parse_measurement(frame: bytes) -> Measurement:
    """A valid frame of length 0x10 into fields. Anything else raises FrameError (the reader drops and counts it)."""
    if len(frame) != MEAS_TOTAL or frame[:2] != HEADER or frame[2] != MEAS_LEN:
        raise FrameError(f"not a measurement frame (length {len(frame)})")
    c = checksum(frame[:-2])
    if frame[-2] != c >> 8 or frame[-1] != c & 0xFF:
        raise FrameError("bad checksum")
    mode, rng = frame[3], frame[4]
    if rng >> 4 != 0x3:
        raise FrameError("range byte has no 0x3 high nibble")
    f0, f1, f2 = frame[14], frame[15], frame[16]
    if any(b >> 4 != 0x3 for b in (f0, f1, f2)):
        raise FrameError("flag byte has no 0x3 high nibble")
    a, b, c2 = f0 & 0xF, f1 & 0xF, f2 & 0xF
    fl = Flags(rel=bool(a & 1), hold=bool(a & 2), min=bool(a & 4), max=bool(a & 8),
               hv=bool(b & 1), low_batt=bool(b & 2), manual_range=bool(b & 4), auto_off=bool(b & 8),
               neg_bar=bool(c2 & 1), pmin=bool(c2 & 2), pmax=bool(c2 & 4), ac=bool(c2 & 8))
    try:
        disp = frame[5:12].decode("ascii")
    except UnicodeDecodeError:
        raise FrameError("display text is not ASCII") from None
    return Measurement(mode, rng & 0xF, disp, fl, (f0, f1, f2), bytes(frame))


def build_measurement(mode: int, range_idx: int, display: str, flags: tuple[int, int, int] = (0x30, 0x30, 0x30), bar: bytes = b"\x00\x00") -> bytes:
    """Test and simulator helper: a measurement frame from its fields, checksum computed."""
    d = display.ljust(7)[:7].encode("ascii")
    return build_frame(bytes([mode, 0x30 | range_idx]) + d + bytes(bar) + bytes(flags))


def parse_name(frame: bytes) -> str | None:
    """The model string from a Get Name answer (payload is ASCII); the acknowledge frame `AB CD 04 FF 00 02 7B` gives None."""
    p = payload_of(frame)
    if len(p) >= 3 and all(32 <= b < 127 for b in p):
        return p.decode("ascii").strip()
    return None
