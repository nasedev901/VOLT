# -*- coding: utf-8 -*-
r"""
VOLT backend - protocol, HID link and mouse operations (no GUI code in this module)

Redragon M711 Cobra (Holtek 04D9:FC30, Pixart PMW3325) on Windows 10/11.

PROTOCOL (reverse-engineered by others; none of it is documented by Redragon)

Sources: dokutan/mouse_m908 (GPL-3.0; M711 listed there as "partial" support),
ernesto-elsaesser/usb-mouse-config and SK2674/m711-port (hardware-checked on an M711).
Treat this file as GPL-3.0 if you redistribute it.

 * The configuration channel is NOT the cursor interface. On the M711 the three USB
   interfaces are MI_00 mouse, MI_01 keyboard, MI_02 vendor "Device" (hiddev). The
   config traffic goes to interface 2, via HID *feature reports*:
        report ID 0x02 -> 16-byte frames       report ID 0x03 -> 64-byte frames
   (first byte of every frame is its report ID, which is how hidapi wants it).
 * Frames:   seek/read  [id F2 addr_lo addr_hi count ...]  then GET_FEATURE ->
                        [id 08 lo hi count 00 FA FA] + data
              write     [id F3 addr_lo addr_hi count 00 00 00 data...]
              unlock    [02 F5 00]      relock  [02 F5 01]
   Every read/write sequence MUST be bracketed by unlock ... relock, otherwise the
   mouse can stop responding (this is what session() guarantees, even on errors).
 * There is no separate "flash commit" packet: writes go straight to the mouse's
   configuration memory, and the final relock frame closes the transaction.
 * Memory map (per profile p = 0..4, the mouse has 5 independent profiles):
        LED      0x0449 + 8p    R G B mode1 speed mode2     (+ brightness @ 0x044F + 8p)
        DPI      0x0042 0x0102 0x01B2 0x0262 0x0312  -> 2 index bytes, then 5 x
                 [enabled, X_lo, X_hi, Y_lo, Y_hi, junk]   (firmware ignores Y)
        POLL     0x0032, 10 bytes, 5 profiles at the even offsets: 1000Hz=1 500=2 250=4 125=8
        ACTIVE   0x002C  (read by default; writing it is OPTIONAL and UNVERIFIED, see the GUI checkbox)
 * DPI is NOT stored in 100-DPI steps:  dpi = X_lo * 44.444 * 2**X_hi  (X_lo 4..140,
   X_hi 0/1). So 800/1600/2400/3200/6400 are exact but 500 is stored as 489, 1300 as 1289, etc.
   The GUI shows the value the mouse will really store next to every stage.


USB restart
The 04D9:FC30 firmware only loads its onboard flash into the active registers at power-up / USB
re-enumeration. apply_config() writes flash; restart_mouse() closes the HID handle, runs
`pnputil /restart-device` on the USB composite parent, waits and reconnects (Administrator needed).
"""
from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

try:
    import hid
    HID_IMPORT_ERROR = None
except Exception as _exc:
    hid = None
    HID_IMPORT_ERROR = _exc

APP_NAME = "VOLT"
__version__ = "1.0.0"
APP_ID = "VOLT.RedragonM711.Configurator"
VID, PID = 0x04D9, 0xFC30
AUTO_LABEL = "Auto (usage page >= 0xFF00, prefers interface 2)"

RID_16, RID_64 = 0x02, 0x03
OP_SEEK, OP_WRITE, OP_LOCK = 0xF2, 0xF3, 0xF5
HEADER_LEN = 8
CAP_16, CAP_64 = 8, 56
FRAME_DELAY = 0.01
SETTLE_DELAY = 0.8

NUM_PROFILES = 5
NUM_STAGES = 5
ADDR_ACTIVE_PROFILE = 0x002C
ADDR_POLL = 0x0032
ADDR_ACTIVE_DPI: Optional[int] = None
REFRESH_DELAY = 0.08

IS_WINDOWS = os.name == "nt"
RESTART_SETTLE = 1.5
RECONNECT_TIMEOUT = 15.0
RECONNECT_POLL = 0.5
PNP_TIMEOUT = 30
USB_CLASS_GUID = "{36fc9e60-c465-11cf-8056-444553540000}"
CREATE_NO_WINDOW = 0x08000000
DPI_BASES = (0x0042, 0x0102, 0x01B2, 0x0262, 0x0312)

DUMP_START, DUMP_END, DUMP_CHUNK = 0x0000, 0x0480, 56

POLL_CODE = {1000: 1, 500: 2, 250: 4, 125: 8}
POLL_HZ = {v: k for k, v in POLL_CODE.items()}

DPI_UNIT = 400.0 / 9.0
DPI_MIN, DPI_MAX = 100, 10000
DPI_CODE_MIN, DPI_CODE_MAX = 0x04, 0x8C
DPI_HIGH_ABOVE = 4933

MODES: Dict[str, Tuple[int, int, bool]] = {
    "Static":       (0x01, 0x02, True),
    "Breathing":    (0x01, 0x04, True),
    "Rainbow":      (0x01, 0x08, False),
    "Wave":         (0x02, 0x00, False),
    "Neon / Cycle": (0x01, 0x01, False),
    "Blink":        (0x01, 0x10, True),
    "Reactive":     (0x07, 0x00, True),
    "Off":          (0x00, 0x00, False),
}
MODE_BY_BYTES = {(a, b): name for name, (a, b, _c) in MODES.items()}
BRIGHT_RANGE = (1, 3)
SPEED_RANGE = (1, 4)

BACKUP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "RedragonM711")

IS_FROZEN = bool(getattr(sys, "frozen", False))


def resource_path(rel: str) -> str:
    """Path of a bundled asset: works from source, onedir and onefile PyInstaller builds."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *rel.replace("\\", "/").split("/"))


def relaunch_as_admin() -> bool:
    """Ask Windows (UAC) to start another copy of this program elevated. True = it was started."""
    if os.name != "nt":
        return False
    try:
        exe = sys.executable
        if IS_FROZEN:
            params = subprocess.list2cmdline(sys.argv[1:])
        else:
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(pyw):
                exe = pyw
            params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(rc) > 32
    except Exception:
        return False


def hexs(data) -> str:
    return " ".join(f"{b:02x}" for b in data)


def led_addr(p: int) -> int:
    return 0x0449 + 8 * p


def led_bright_addr(p: int) -> int:
    return 0x044F + 8 * p


def dpi_rec_addr(p: int, i: int) -> int:
    return DPI_BASES[p] + 2 + 6 * i


def code_to_dpi(lo: int, hi: int) -> int:
    return int(round(lo * DPI_UNIT * (1 << (hi & 1))))


def dpi_to_code(dpi: int) -> Tuple[int, int, int]:
    """Return (X_lo, X_hi, effective_dpi) for a requested DPI."""
    dpi = max(DPI_MIN, min(DPI_MAX, int(dpi)))
    hi = 1 if dpi > DPI_HIGH_ABOVE else 0
    lo = int(round(dpi / (DPI_UNIT * (1 << hi))))
    lo = max(DPI_CODE_MIN, min(DPI_CODE_MAX, lo))
    return lo, hi, code_to_dpi(lo, hi)


def snap_dpi(value: int) -> int:
    return max(DPI_MIN, min(DPI_MAX, int(int(value) / 100.0 + 0.5) * 100))


def normalize_hex(text: str) -> Optional[str]:
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        return None
    try:
        int(t, 16)
    except ValueError:
        return None
    return "#" + t.lower()


def hex_to_rgb(text: str) -> Tuple[int, int, int]:
    h = normalize_hex(text) or "#ff0000"
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def _pad(buf, size: int) -> bytes:
    if len(buf) > size:
        raise ValueError("frame too long")
    return bytes(buf) + bytes(size - len(buf))


def report_for(n: int) -> Tuple[int, int]:
    if n <= CAP_16:
        return RID_16, 16
    if n <= CAP_64:
        return RID_64, 64
    raise ValueError("too many data bytes: %d" % n)


def build_seek(addr: int, n: int) -> bytes:
    rid, size = report_for(n)
    return _pad([rid, OP_SEEK, addr & 0xFF, (addr >> 8) & 0xFF, n], size)


def build_write(addr: int, data) -> bytes:
    data = bytes(data)
    rid, size = report_for(len(data))
    return _pad([rid, OP_WRITE, addr & 0xFF, (addr >> 8) & 0xFF, len(data), 0, 0, 0] + list(data), size)


def build_lock(lock: bool) -> bytes:
    return _pad([RID_16, OP_LOCK, 1 if lock else 0], 16)


class LinkError(Exception):
    pass


class ProtocolError(Exception):
    pass


def _path_text(info) -> str:
    p = info.get("path", b"")
    return p.decode("utf-8", "ignore") if isinstance(p, (bytes, bytearray)) else str(p)


def endpoint_label(n: int, info) -> str:
    """Short one-line description of a HID collection for the endpoint dropdown."""
    m = re.search(r"mi_\d+(?:&col\d+)?", _path_text(info).lower())
    tail = m.group(0).upper() if m else "?"
    return "[%d] if=%s  up=0x%04X  u=0x%04X  %s" % (
        n, info.get("interface_number"), info.get("usage_page") or 0, info.get("usage") or 0, tail)


def pick_config_interface(infos, override: Optional[int] = None):
    """Choose the vendor configuration interface (MI_02 on this mouse)."""
    best, best_score = None, 0
    for info in infos:
        score = 0
        if override is not None:
            if info.get("interface_number") == override:
                score = 1000
        else:
            if (info.get("usage_page") or 0) >= 0xFF00:
                score += 100
            if info.get("interface_number") == 2:
                score += 10
            if "mi_02" in _path_text(info).lower():
                score += 5
        if score > best_score:
            best, best_score = info, score
    return best


def _open_device(path):
    if hasattr(hid, "device"):
        dev = hid.device()
        dev.open_path(path)
        return dev
    if hasattr(hid, "Device"):
        return hid.Device(path=path)
    raise LinkError("Unsupported 'hid' module - run: py -m pip install hidapi")


class MouseLink:
    def __init__(self, log: Callable[[str], None], dry_run: bool = False,
                 interface_override: Optional[int] = None, path_override=None):
        self.log = log
        self.dry_run = dry_run
        self.interface_override = interface_override
        self.path_override = path_override
        self.dev = None
        self.info = None
        self.pad_to: Optional[int] = None
        self._last_seek: Optional[Tuple[int, int, int]] = None
        self._io = threading.RLock()
        self._depth = 0
        self.writes = 0

    def list_interfaces(self):
        if hid is None:
            return []
        return list(hid.enumerate(VID, PID))

    def is_present(self) -> bool:
        if self.dry_run:
            return True
        try:
            return bool(self.list_interfaces())
        except Exception:
            return False

    def open(self):
        if self.dry_run:
            self.log("[dry-run] no device is opened, frames are only logged")
            return
        if hid is None:
            raise LinkError("Python module 'hid' is missing (%s). Run: py -m pip install hidapi"
                            % HID_IMPORT_ERROR)
        infos = self.list_interfaces()
        if not infos:
            raise LinkError("No 04D9:FC30 device found")
        if self.path_override is not None:
            info = next((i for i in infos if i.get("path") == self.path_override), None)
            if info is None:
                raise LinkError("The selected HID endpoint is no longer present - press Refresh.")
        else:
            info = pick_config_interface(infos, self.interface_override)
        if info is None:
            raise LinkError("04D9:FC30 found but no vendor config interface (usage page >= 0xFF00 / "
                            "interface 2). Run with --debug and send me the output.")
        try:
            self.dev = _open_device(info["path"])
        except Exception as exc:
            raise LinkError("Could not open %s: %s  (close the official Redragon software; "
                            "if it persists try 'Run as administrator')" % (_path_text(info), exc))
        self.info = info
        self.pad_to = None
        self.log("opened interface %s usage_page=0x%04X usage=0x%04X  %s" % (
            info.get("interface_number"), info.get("usage_page") or 0, info.get("usage") or 0,
            _path_text(info)))

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.info = None

    def _err(self) -> str:
        try:
            return str(self.dev.error())
        except Exception:
            return "?"

    def _send(self, frame: bytes):
        self.log("TX[%02d] %s" % (len(frame), hexs(frame)))
        if self.dry_run:
            if frame[1] == OP_SEEK:
                self._last_seek = (frame[0], frame[2] | (frame[3] << 8), frame[4])
            return
        if self.dev is None:
            raise LinkError("device is not open")
        if self.pad_to and len(frame) < self.pad_to:
            attempts = [_pad(frame, self.pad_to)]
        else:
            attempts = [frame]
            if len(frame) < 64:
                attempts.append(_pad(frame, 64))
        last = None
        for buf in attempts:
            try:
                r = self.dev.send_feature_report(buf)
                self.log("     send_feature_report(len=%d) -> %r" % (len(buf), r))
                if isinstance(r, int) and r < 0:
                    raise IOError("send_feature_report returned %d (%s)" % (r, self._err()))
                if len(buf) != len(frame):
                    self.pad_to = len(buf)
                    self.log("(Windows needed %d-byte padding; remembered)" % len(buf))
                if frame[1] == OP_SEEK:
                    self._last_seek = (frame[0], frame[2] | (frame[3] << 8), frame[4])
                return
            except Exception as exc:
                last = exc
                self.log("HID ERROR (len %d): %r" % (len(buf), exc))
        raise LinkError("send_feature_report failed: %s" % last)

    def _get(self, rid: int, size: int) -> bytes:
        if self.dry_run:
            _rid, addr, n = self._last_seek or (rid, 0, 0)
            hdr = [rid, 0x08, addr & 0xFF, ((addr >> 8) + 0x60) & 0xFF, n, 0x00, 0xFA, 0xFA]
            data = _pad(hdr, size)
            self.log("RX[%02d] %s  (dry-run synthetic)" % (len(data), hexs(data)))
            return data
        if self.dev is None:
            raise LinkError("device is not open")
        want = self.pad_to or size
        try:
            data = bytes(self.dev.get_feature_report(rid, want))
        except Exception as exc:
            self.log("HID ERROR (get): %r" % (exc,))
            raise LinkError("get_feature_report failed: %s" % exc)
        self.log("     get_feature_report(id=0x%02x, size=%d) -> %d bytes" % (rid, want, len(data)))
        self.log("RX[%02d] %s" % (len(data), hexs(data)))
        if not data:
            raise LinkError("empty feature report (%s)" % self._err())
        if not (len(data) > 1 and data[0] == rid and data[1] == 0x08) and data[0] == 0x08:
            data = bytes([rid]) + data
        return data

    @contextlib.contextmanager
    def session(self):
        """unlock ... relock.  The relock is ALWAYS sent, even after an error."""
        with self._io:
            if self._depth > 0:
                yield
                return
            self._send(build_lock(False))
            time.sleep(FRAME_DELAY)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                try:
                    self._send(build_lock(True))
                except Exception as exc:
                    self.log("!!! FAILED TO RE-LOCK THE MOUSE: %s  (unplug/replug it)" % exc)

    def read(self, addr: int, n: int) -> bytes:
        with self._io:
            rid, size = report_for(n)
            self._send(build_seek(addr, n))
            time.sleep(FRAME_DELAY)
            resp = self._get(rid, size)
            hdr = resp[:HEADER_LEN]
            if len(resp) < HEADER_LEN + n or hdr[1] != 0x08 or hdr[6:8] != b"\xfa\xfa" or hdr[4] != n:
                raise ProtocolError("unexpected reply header %s for read of %d bytes at 0x%04X"
                                    % (hexs(hdr), n, addr))
            return resp[HEADER_LEN:HEADER_LEN + n]

    def write(self, addr: int, data) -> None:
        with self._io:
            self.writes += 1
            self._send(build_write(addr, data))
            time.sleep(FRAME_DELAY)


@dataclasses.dataclass
class DpiStage:
    enabled: bool
    dpi: int


@dataclasses.dataclass
class LedSettings:
    mode: str
    rgb: Tuple[int, int, int]
    brightness: int
    speed: int


@dataclasses.dataclass
class Config:
    stages: List[DpiStage]
    led: LedSettings
    poll_hz: int
    write_poll: bool = True


def handshake(link: MouseLink) -> int:
    """Read the active profile; proves the frame format works. Returns profile 0..4."""
    with link.session():
        d = link.read(ADDR_ACTIVE_PROFILE, 1)
    if not 0 <= d[0] < NUM_PROFILES:
        raise ProtocolError("active profile byte out of range: 0x%02x" % d[0])
    return d[0]


def read_profile_raw(link: MouseLink, p: int) -> Dict[str, List[int]]:
    return {"led": list(link.read(led_addr(p), 7)), "dpi": list(link.read(DPI_BASES[p], 32))}


def read_poll_raw(link: MouseLink) -> List[int]:
    return list(link.read(ADDR_POLL, 10))


def decode_led(raw7) -> Tuple[Optional[str], Tuple[int, int, int], int, int]:
    r, g, b, m1, speed, m2, bright = raw7[:7]
    return MODE_BY_BYTES.get((m1, m2)), (r, g, b), bright, speed


def decode_dpi(raw32) -> List[DpiStage]:
    out = []
    for i in range(NUM_STAGES):
        rec = raw32[2 + 6 * i: 8 + 6 * i]
        out.append(DpiStage(rec[0] == 1, code_to_dpi(rec[1], rec[2])))
    return out


def decode_poll(raw10, p: int) -> Optional[int]:
    return POLL_HZ.get(raw10[2 * p])


def write_led(link: MouseLink, p: int, led: LedSettings, keep_rgb, current=None) -> bool:
    """Returns True if anything was written to the mouse."""
    wrote = False
    m1, m2, uses_color = MODES[led.mode]
    r, g, b = led.rgb if uses_color else tuple(keep_rgb)
    speed = max(SPEED_RANGE[0], min(SPEED_RANGE[1], int(led.speed)))
    bright = max(BRIGHT_RANGE[0], min(BRIGHT_RANGE[1], int(led.brightness)))
    new6 = [r, g, b, m1, speed, m2]
    if current is not None and list(current[:6]) == new6:
        link.log("profile %d LED record unchanged -> not rewritten" % (p + 1))
    else:
        link.write(led_addr(p), new6)
        wrote = True
    if current is not None and len(current) > 6 and current[6] == bright:
        link.log("profile %d brightness unchanged -> not rewritten" % (p + 1))
    else:
        link.write(led_bright_addr(p), [bright])
        wrote = True
    return wrote


def write_dpi(link: MouseLink, p: int, stages: List[DpiStage], current32=None) -> bool:
    """Returns True if anything was written to the mouse."""
    wrote = False
    for i, st in enumerate(stages):
        lo, hi, _eff = dpi_to_code(st.dpi)
        rec = [1 if st.enabled else 0, lo, hi, lo, hi, 0]
        if current32 is not None and list(current32[2 + 6 * i: 7 + 6 * i]) == rec[:5]:
            continue
        if current32 is not None and st.enabled and (current32[2 + 6 * i + 2] & 1) and not hi:
            mid = max(DPI_CODE_MIN, min(DPI_CODE_MAX, int(round(lo / 2.0))))
            link.log("stage %d: x2 -> x1 drop, stepping via lo=%d at x2 first" % (i + 1, mid))
            link.write(dpi_rec_addr(p, i), [1, mid, 1, mid, 1, 0])
            time.sleep(0.15)
        link.write(dpi_rec_addr(p, i), rec)
        wrote = True
    return wrote


def save_backup(data: dict, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, "backup-%s.json" % stamp)
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(backup_dir, "backup-%s-%d.json" % (stamp, n))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    return path


def dump_memory(link: MouseLink, start: int = DUMP_START, end: int = DUMP_END,
                chunk: int = DUMP_CHUNK) -> Dict[int, Optional[bytes]]:
    """Read config memory [start, end) in chunks. Unreadable chunks are stored as None."""
    out: Dict[int, Optional[bytes]] = {}
    with link.session():
        addr = start
        while addr < end:
            n = min(chunk, end - addr)
            try:
                out[addr] = link.read(addr, n)
            except (LinkError, ProtocolError) as exc:
                link.log("dump: 0x%04X..0x%04X unreadable (%s)" % (addr, addr + n - 1, exc))
                out[addr] = None
            addr += n
    return out


def flatten_dump(dump: Dict[int, Optional[bytes]]) -> Dict[int, int]:
    flat: Dict[int, int] = {}
    for addr, data in dump.items():
        if data is None:
            continue
        for i, b in enumerate(data):
            flat[addr + i] = b
    return flat


def dump_to_text(dump: Dict[int, Optional[bytes]]) -> str:
    flat = flatten_dump(dump)
    if not flat:
        return "(nothing could be read)"
    lo, hi = min(flat) & ~0xF, max(flat) + 1
    lines = []
    for row in range(lo, hi, 16):
        cells = ["%02x" % flat[a] if a in flat else ".." for a in range(row, min(row + 16, hi))]
        lines.append("%04x: %s" % (row, " ".join(cells)))
    return "\n".join(lines)


def diff_dumps(before, after) -> List[Tuple[int, bytes, bytes]]:
    """Return [(start_addr, old_bytes, new_bytes)] for every run of changed bytes."""
    fa, fb = flatten_dump(before), flatten_dump(after)
    changed = sorted(a for a in fb if a in fa and fa[a] != fb[a])
    groups: List[List[int]] = []
    for a in changed:
        if groups and a == groups[-1][-1] + 1:
            groups[-1].append(a)
        else:
            groups.append([a])
    return [(g[0], bytes(fa[a] for a in g), bytes(fb[a] for a in g)) for g in groups]


def format_diff(diff) -> List[str]:
    return ["0x%04X  %s  ->  %s" % (a, hexs(old), hexs(new)) for a, old, new in diff]


def save_dump(dump, label: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, "dump-%s-%s.txt" % (label, stamp))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(backup_dir, "dump-%s-%s-%d.txt" % (label, stamp, n))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dump_to_text(dump) + "\n")
    return path


def refresh_active(link: MouseLink, cfg: Config, target_profile: Optional[int] = None) -> List[str]:
    """Make the mouse re-load what it is *currently using* (must run inside link.session()).

    The mouse only re-reads LED / DPI settings from memory when the active profile or the active
    DPI stage CHANGES (that is what the physical button does). Writing the same value again is a
    no-op, so we bounce: write a neighbour, then write the real value.

      1. active profile byte (0x002C): bounce -> LEDs + stage table are reloaded
      2. active DPI stage byte (only if ADDR_ACTIVE_DPI is known): bounce -> sensor switches DPI

    There is no separate '08 02' commit frame on this protocol; the relock frame sent when the
    session ends is the final commit. Returns a list of read-back mismatches (empty = OK).
    """
    problems: List[str] = []
    cur = link.read(ADDR_ACTIVE_PROFILE, 1)[0]
    if not 0 <= cur < NUM_PROFILES:
        cur = 0
    tgt = cur if target_profile is None else target_profile
    alt = (tgt + 1) % NUM_PROFILES
    link.log("refresh: active profile byte %d -> %d -> %d" % (cur, alt, tgt))
    link.write(ADDR_ACTIVE_PROFILE, [alt])
    time.sleep(REFRESH_DELAY)
    link.write(ADDR_ACTIVE_PROFILE, [tgt])
    time.sleep(REFRESH_DELAY)

    if ADDR_ACTIVE_DPI is not None:
        enabled = [i for i, st in enumerate(cfg.stages) if st.enabled]
        stage = link.read(ADDR_ACTIVE_DPI, 1)[0]
        want = stage if stage in enabled else enabled[0]
        others = [i for i in enabled if i != want]
        if others:
            link.log("refresh: active DPI stage byte %d -> %d -> %d" % (stage, others[0], want))
            link.write(ADDR_ACTIVE_DPI, [others[0]])
            time.sleep(REFRESH_DELAY)
        else:
            link.log("refresh: only one DPI stage enabled, writing it directly (%d)" % want)
        link.write(ADDR_ACTIVE_DPI, [want])
        time.sleep(REFRESH_DELAY)
        got = link.read(ADDR_ACTIVE_DPI, 1)[0]
        if got != want and not link.dry_run:
            problems.append("active DPI stage byte: wrote %d, read %d" % (want, got))
    else:
        link.log("refresh: ADDR_ACTIVE_DPI unknown -> DPI stage not nudged (see --find-active)")

    got_p = link.read(ADDR_ACTIVE_PROFILE, 1)[0]
    if got_p != tgt and not link.dry_run:
        problems.append("active profile byte after refresh: wrote %d, read %d" % (tgt, got_p))
    return problems


def apply_config(link: MouseLink, cfg: Config, profiles: List[int], verify: bool = True,
                 backup_dir: Optional[str] = BACKUP_DIR, dump: bool = False,
                 set_active: Optional[int] = None, refresh: bool = True) -> dict:
    """Backup -> write -> (optional) read-back verification. Returns a result dict.

    dump        also dump config memory before and after and log the byte diff.
    set_active  if not None, also write this profile index (0..4) to the active-profile byte
                (0x002C). EXPERIMENTAL: that write is not verified against real hardware.
    refresh     bounce the active profile (and the active DPI stage if its address is known)
                right before the final relock, so LED + DPI changes apply without a button press.
    """
    if not any(s.enabled for s in cfg.stages):
        raise ValueError("At least one DPI stage must stay enabled "
                         "(all stages disabled can make the mouse unresponsive).")
    if cfg.led.mode not in MODES:
        raise ValueError("unknown lighting mode %r" % cfg.led.mode)
    if set_active is not None and not 0 <= set_active < NUM_PROFILES:
        raise ValueError("active profile index out of range: %r" % set_active)

    with link.session():
        backup = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                  "profiles": {str(p): read_profile_raw(link, p) for p in profiles},
                  "poll": read_poll_raw(link)}
    backup_path = save_backup(backup, backup_dir) if backup_dir else None
    link.log("backup saved: %s" % backup_path)

    dump_before = dump_memory(link) if dump else None
    dump_files: List[str] = []
    if dump_before is not None and backup_dir:
        dump_files.append(save_dump(dump_before, "before", backup_dir))

    x2_drop = False
    for p in profiles:
        old_dpi = backup["profiles"][str(p)]["dpi"]
        for i, st in enumerate(cfg.stages):
            if (old_dpi[2 + 6 * i + 2] & 1) and not dpi_to_code(st.dpi)[1]:
                x2_drop = True
    if x2_drop and refresh:
        link.log("x2 -> x1 DPI drop detected: live profile bounce SKIPPED (it can freeze the mouse); "
                 "restart the mouse (button / replug) to load the new values")
        refresh = False

    poll_written = None
    changed = False
    dpi_changed = False
    refresh_problems: List[str] = []
    with link.session():
        for p in profiles:
            old = backup["profiles"][str(p)]
            changed |= write_led(link, p, cfg.led, old["led"][:3], current=old["led"])
            wrote_dpi = write_dpi(link, p, cfg.stages, current32=old["dpi"])
            dpi_changed |= wrote_dpi
            changed |= wrote_dpi
        if cfg.write_poll:
            poll_old = read_poll_raw(link)
            poll = list(poll_old)
            for p in profiles:
                poll[2 * p] = POLL_CODE[cfg.poll_hz]
            if poll != poll_old:
                link.write(ADDR_POLL, bytes(poll))
                changed = True
            else:
                link.log("polling rate unchanged -> not rewritten")
            poll_written = poll
        if set_active is not None:
            changed = True
            link.log("EXPERIMENTAL: writing active-profile byte 0x%04X = %d" % (ADDR_ACTIVE_PROFILE, set_active))
            link.write(ADDR_ACTIVE_PROFILE, [set_active])
        if refresh and not dpi_changed:
            refresh_problems = refresh_active(link, cfg, set_active)

    if not link.dry_run:
        time.sleep(SETTLE_DELAY)

    mismatches: List[str] = []
    if verify and not link.dry_run:
        with link.session():
            for p in profiles:
                raw = read_profile_raw(link, p)
                m1, m2, uses_color = MODES[cfg.led.mode]
                want_rgb = list(cfg.led.rgb) if uses_color else backup["profiles"][str(p)]["led"][:3]
                want_led = want_rgb + [m1, max(SPEED_RANGE[0], min(SPEED_RANGE[1], cfg.led.speed)), m2,
                                       max(BRIGHT_RANGE[0], min(BRIGHT_RANGE[1], cfg.led.brightness))]
                if raw["led"] != want_led:
                    mismatches.append("profile %d LED: wrote %s, read %s" % (p + 1, hexs(want_led), hexs(raw["led"])))
                for i, st in enumerate(cfg.stages):
                    lo, hi, _ = dpi_to_code(st.dpi)
                    rec = raw["dpi"][2 + 6 * i: 5 + 6 * i]
                    if rec != [1 if st.enabled else 0, lo, hi]:
                        mismatches.append("profile %d DPI stage %d: wrote %s, read %s" % (
                            p + 1, i + 1, hexs([1 if st.enabled else 0, lo, hi]), hexs(rec)))
            if poll_written is not None:
                now = read_poll_raw(link)
                for p in profiles:
                    if now[2 * p] != poll_written[2 * p]:
                        mismatches.append("profile %d polling rate: wrote %d, read %d" % (
                            p + 1, poll_written[2 * p], now[2 * p]))
            if set_active is not None:
                now_active = link.read(ADDR_ACTIVE_PROFILE, 1)[0]
                link.log("active-profile byte now reads %d (wrote %d)" % (now_active, set_active))
                if now_active != set_active:
                    mismatches.append("active profile byte: wrote %d, read %d" % (set_active, now_active))
    mismatches += refresh_problems
    for m in mismatches:
        link.log("VERIFY MISMATCH: " + m)

    diff_lines: List[str] = []
    if dump_before is not None:
        dump_after = dump_memory(link)
        if backup_dir:
            dump_files.append(save_dump(dump_after, "after", backup_dir))
        diff_lines = format_diff(diff_dumps(dump_before, dump_after))
        link.log("MEMORY DIFF (%d changed run(s)):" % len(diff_lines))
        for line in diff_lines:
            link.log("   " + line)
        if not diff_lines:
            link.log("   (no byte in 0x%04X-0x%04X changed)" % (DUMP_START, DUMP_END - 1))
    return {"backup": backup_path, "mismatches": mismatches, "profiles": profiles,
            "diff": diff_lines, "dump_files": dump_files, "x2_drop": x2_drop,
            "changed": changed, "dpi_changed": dpi_changed}


class RestartError(Exception):
    """The USB restart could not be done (not admin, device not found, pnputil failed ...)."""


_PARENT_RE = re.compile(r"USB\\VID_%04X&PID_%04X\\\S+" % (VID, PID), re.IGNORECASE)


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode_console(raw: bytes) -> str:
    for enc in ("mbcs", "utf-8"):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1", "replace")


def _run_hidden(cmd: List[str], timeout: float) -> Tuple[int, str]:
    """Run a console tool without flashing a window. Returns (exit code, combined output)."""
    kw = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        cp = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, **kw)
    except FileNotFoundError:
        raise RestartError("'%s' was not found - the USB restart only works on Windows." % cmd[0])
    except subprocess.TimeoutExpired:
        raise RestartError("'%s' did not finish within %d s." % (cmd[0], timeout))
    return cp.returncode, _decode_console(cp.stdout or b"").strip()


def find_usb_parent_ids(log: Callable[[str], None]) -> List[str]:
    r"""Instance IDs of the USB *composite parent* (USB\VID_04D9&PID_FC30\<id>) - NOT the
    USB\...&MI_xx interfaces or the HID\... children, so the whole mouse is restarted.
    PowerShell first, `pnputil /enum-devices` as a fallback."""
    ids: List[str] = []
    ps = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
          "Where-Object { $_.InstanceId -like 'USB\\VID_%04X&PID_%04X\\*' } | "
          "ForEach-Object { $_.InstanceId }" % (VID, PID))
    try:
        _rc, out = _run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-Command", ps], PNP_TIMEOUT)
        ids = [m.group(0) for m in _PARENT_RE.finditer(out)]
    except RestartError as exc:
        log("PowerShell device lookup failed: %s" % exc)
    if not ids:
        log("falling back to: pnputil /enum-devices /connected /class %s" % USB_CLASS_GUID)
        _rc, out = _run_hidden(["pnputil", "/enum-devices", "/connected", "/class", USB_CLASS_GUID], PNP_TIMEOUT)
        ids = [m.group(0) for m in _PARENT_RE.finditer(out)]
    ids = list(dict.fromkeys(ids))
    log("USB parent device(s): %s" % (", ".join(ids) if ids else "none"))
    return ids


def pnputil_restart(instance_id: str, log: Callable[[str], None]) -> None:
    log('pnputil /restart-device "%s"' % instance_id)
    rc, out = _run_hidden(["pnputil", "/restart-device", instance_id], PNP_TIMEOUT)
    for line in out.splitlines():
        log("   pnputil: %s" % line)
    if rc == 0:
        return
    low = out.lower()
    if rc in (5, 740) or "access is denied" in low or "administrator" in low or "elevat" in low:
        raise RestartError("Windows refused to restart the mouse: administrator rights are required.\n"
                           "Close this program and start it again with 'Run as administrator'.")
    if rc == 3010:
        raise RestartError("Windows says a full reboot is needed to restart this device (pnputil exit 3010).")
    raise RestartError("pnputil /restart-device failed (exit code %d): %s\n"
                       "(this command needs Windows 10 version 1903 or newer)" % (rc, out or "no output"))


def reconnect(link: MouseLink, saved, timeout: float = RECONNECT_TIMEOUT) -> int:
    """Reopen the config interface after a restart, retrying until the mouse is back.
    `saved` = (interface_number, usage_page, usage) of the endpoint that was open before."""
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while True:
        try:
            link.open()
            return handshake(link)
        except (LinkError, ProtocolError) as exc:
            last = exc
            link.close()
            if link.path_override is not None:
                try:
                    infos = link.list_interfaces()
                except Exception:
                    infos = []
                if not any(i.get("path") == link.path_override for i in infos):
                    match = next((i for i in infos if saved and (i.get("interface_number"), i.get("usage_page"),
                                                                 i.get("usage")) == saved), None)
                    link.path_override = match["path"] if match else None
        if time.time() >= deadline:
            raise LinkError("The mouse did not come back within %.0f s after the restart (%s). "
                            "Unplug and replug it, then press Rescan." % (timeout, last))
        time.sleep(RECONNECT_POLL)


def restart_mouse(link: MouseLink, settle: float = RESTART_SETTLE) -> int:
    """Close the HID handle -> pnputil /restart-device on the USB composite parent -> wait ->
    reopen MouseLink. Returns the active profile read after reconnecting.

    Raises RestartError if the restart could not be done (then the link is still open or has been
    reopened), LinkError if the mouse did not come back."""
    log = link.log
    if link.dry_run:
        log("[dry-run] would close HID, run pnputil /restart-device on USB\\VID_%04X&PID_%04X\\..., "
            "wait %.1f s and reopen" % (VID, PID, settle))
        return handshake(link)
    if not IS_WINDOWS:
        raise RestartError("The USB restart uses pnputil and only works on Windows.")
    if not is_admin():
        raise RestartError("This program is NOT running as Administrator, so Windows will not let it restart "
                           "the mouse.\nClose it and start it again with 'Run as administrator' "
                           "(your settings are already saved in the mouse).")
    ids = find_usb_parent_ids(log)
    if not ids:
        raise RestartError("Could not find the mouse's USB device (USB\\VID_%04X&PID_%04X\\...) in Windows." % (VID, PID))
    if len(ids) > 1:
        log("more than one USB parent found, using the first: %s" % ids[0])
    info = link.info or {}
    saved = (info.get("interface_number"), info.get("usage_page"), info.get("usage")) if info else None

    with link._io:
        link.close()
    log("HID handle closed")

    err: Optional[RestartError] = None
    try:
        pnputil_restart(ids[0], log)
        log("waiting %.1f s for the mouse to re-enumerate..." % settle)
        time.sleep(settle)
    except RestartError as exc:
        err = exc
        time.sleep(0.3)
    try:
        profile = reconnect(link, saved)
    except LinkError as exc:
        if err is not None:
            raise RestartError("%s\n\nAlso could not reopen the mouse afterwards: %s" % (err, exc))
        raise
    log("reconnected, active profile byte = %d" % profile)
    if err is not None:
        raise err
    return profile


SETTINGS_FILE = "settings.json"


def load_settings(backup_dir: str = BACKUP_DIR) -> dict:
    try:
        with open(os.path.join(backup_dir, SETTINGS_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict, backup_dir: str = BACKUP_DIR) -> None:
    try:
        os.makedirs(backup_dir, exist_ok=True)
        with open(os.path.join(backup_dir, SETTINGS_FILE), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        pass


def restore_backup(link: MouseLink, path: str) -> int:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    count = 0
    with link.session():
        for key, raw in data["profiles"].items():
            p = int(key)
            link.write(led_addr(p), raw["led"][:6])
            link.write(led_bright_addr(p), [raw["led"][6]])
            for i in range(NUM_STAGES):
                link.write(dpi_rec_addr(p, i), raw["dpi"][2 + 6 * i: 8 + 6 * i])
            count += 1
        if data.get("poll"):
            link.write(ADDR_POLL, bytes(data["poll"]))
    return count


def recover_led(link: MouseLink, profiles: List[int]) -> None:
    """Documented recovery for a frozen LED animation: Static + white."""
    with link.session():
        for p in profiles:
            write_led(link, p, LedSettings("Static", (255, 255, 255), 3, 2), (255, 255, 255))


def cmd_debug(args) -> int:
    if hid is None:
        print("Python module 'hid' is not available: %s" % HID_IMPORT_ERROR)
        print("Install it with:  py -m pip install hidapi")
        return 2
    infos = list(hid.enumerate(VID, PID))
    if not infos:
        print("No HID interfaces found for %04X:%04X." % (VID, PID))
        others = list(hid.enumerate(VID, 0))
        if others:
            print("Other Holtek (04D9) HID devices that are present:")
            for o in others:
                print("   PID %04X  %s  interface=%s usage_page=0x%04X" % (
                    o.get("product_id", 0), o.get("product_string"), o.get("interface_number"),
                    o.get("usage_page") or 0))
        return 1
    chosen = pick_config_interface(infos, args.interface)
    print("Found %d HID interface(s) for %04X:%04X\n" % (len(infos), VID, PID))
    for n, info in enumerate(infos):
        mark = "  <== config interface" if info is chosen else ""
        print("[%d]%s" % (n, mark))
        print("    interface_number : %s" % info.get("interface_number"))
        print("    usage_page       : 0x%04X" % (info.get("usage_page") or 0))
        print("    usage            : 0x%04X" % (info.get("usage") or 0))
        print("    release_number   : 0x%04X" % (info.get("release_number") or 0))
        print("    manufacturer     : %s" % info.get("manufacturer_string"))
        print("    product          : %s" % info.get("product_string"))
        print("    path             : %s" % _path_text(info))
    if chosen is None:
        print("\nNo vendor config interface could be identified (expected usage_page >= 0xFF00 / MI_02).")
        return 1
    print("\nSelected: interface %s, usage page 0x%04X." % (chosen.get("interface_number"),
                                                          chosen.get("usage_page") or 0))
    print("If opening fails with 'access denied', close the official Redragon software and "
          "try an Administrator prompt.")
    return 0


def cmd_probe(args) -> int:
    link = MouseLink(print, dry_run=False, interface_override=args.interface)
    try:
        link.open()
        p = handshake(link)
        print("Handshake OK. Mouse reports active profile %d." % (p + 1))
        with link.session():
            raw = read_profile_raw(link, p)
            poll = read_poll_raw(link)
        mode, rgb, bright, speed = decode_led(raw["led"])
        print("LED   : mode=%s rgb=%s brightness=%d speed=%d" % (mode, rgb_to_hex(rgb), bright, speed))
        for i, st in enumerate(decode_dpi(raw["dpi"])):
            print("DPI %d : %s %d" % (i + 1, "on " if st.enabled else "off", st.dpi))
        print("Poll  : %s Hz" % decode_poll(poll, p))
        return 0
    except (LinkError, ProtocolError) as exc:
        print("PROBE FAILED: %s" % exc)
        return 1
    finally:
        link.close()


def cmd_dump(args) -> int:
    link = MouseLink(lambda _m: None, dry_run=False, interface_override=args.interface)
    try:
        link.open()
        handshake(link)
        d = dump_memory(link)
        path = save_dump(d, "manual", BACKUP_DIR)
        print(dump_to_text(d))
        print("\nsaved: %s" % path)
        return 0
    except (LinkError, ProtocolError) as exc:
        print("DUMP FAILED: %s" % exc)
        return 1
    finally:
        link.close()


def cmd_find_active(args) -> int:
    """Interactive, write-free helper: finds which memory bytes change when you press the
    mouse's DPI / mode buttons (= the 'active DPI stage' / 'active profile' bytes)."""
    link = MouseLink(lambda _m: None, dry_run=False, interface_override=args.interface)
    try:
        if sys.stdin is None:
            print("--find-active is interactive and needs a console. Run it from source:  py volt.py --find-active")
            return 2
        link.open()
        handshake(link)
        prev = dump_memory(link)
        print("Baseline dump taken. Nothing is written by this command.")
        while True:
            what = input("\nPress the mouse button you want to locate ONCE (e.g. DPI), then type its "
                         "name here and Enter (blank = quit): ").strip()
            if not what:
                break
            cur = dump_memory(link)
            lines = format_diff(diff_dumps(prev, cur))
            print("Bytes that changed after '%s':" % what)
            print("\n".join("   " + l for l in lines) if lines else "   (none - try again)")
            prev = cur
        print("\nIf ONE byte changed 0->1->2... on repeated DPI presses, start the GUI with\n"
              "   --active-dpi-addr 0xNNNN   (that address).")
        return 0
    except (LinkError, ProtocolError) as exc:
        print("FAILED: %s" % exc)
        return 1
    finally:
        link.close()
