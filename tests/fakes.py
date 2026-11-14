"""Test doubles: a simulated M711 behind a fake `hid` module, and a strict stub of customtkinter / tkinter."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import types

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="volt-test-appdata-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UNIT = 400.0 / 9.0


def _dpi_of(lo, hi):
    return lo * UNIT * (1 << (hi & 1))


class FakeMouse:
    PATH = b"\\\\?\\hid#vid_04d9&pid_fc30&mi_02#8&1234&0&0000#{4d1e55b2}"

    def __init__(self):
        self.mem = bytearray(0x600)
        self.unlocked = False
        self.pending = None
        self.write_log = []
        self.violations = []
        self.hung = False
        self.lock = threading.RLock()
        self.mem[0x2C] = 0
        bases = (0x0042, 0x0102, 0x01B2, 0x0262, 0x0312)
        stages = [(1, 18, 0), (1, 36, 0), (1, 54, 0), (1, 72, 0), (1, 112, 1)]
        for p in range(5):
            a = 0x449 + 8 * p
            self.mem[a:a + 7] = bytes([255, 0, 0, 0x01, 2, 0x02, 3])
            b = bases[p]
            for i, (en, lo, hi) in enumerate(stages):
                self.mem[b + 2 + 6 * i:b + 8 + 6 * i] = bytes([en, lo, hi, lo, hi, 0])
        for p in range(5):
            self.mem[0x32 + 2 * p] = 2

    def send_feature_report(self, buf):
        with self.lock:
            buf = bytes(buf)
            rid, op = buf[0], buf[1]
            if op == 0xF5:
                self.unlocked = buf[2] == 0
                return len(buf)
            if not self.unlocked:
                self.violations.append("frame %02x outside unlock/relock" % op)
            if op == 0xF2:
                self.pending = (rid, buf[2] | (buf[3] << 8), buf[4])
            elif op == 0xF3:
                addr, n = buf[2] | (buf[3] << 8), buf[4]
                data = buf[8:8 + n]
                self._guard(addr, data)
                self.mem[addr:addr + n] = data
                self.write_log.append((addr, bytes(data)))
            return len(buf)

    def _guard(self, addr, data):
        """Firmware freeze seen on the real mouse: x2 -> x1 in ONE jump while the DPI value changes."""
        bases = (0x0042, 0x0102, 0x01B2, 0x0262, 0x0312)
        for b in bases:
            for i in range(5):
                rec = b + 2 + 6 * i
                if addr == rec and len(data) == 6:
                    old_lo, old_hi = self.mem[rec + 1], self.mem[rec + 2]
                    new_lo, new_hi = data[1], data[2]
                    if (old_hi & 1) and not (new_hi & 1):
                        o, n = _dpi_of(old_lo, old_hi), _dpi_of(new_lo, new_hi)
                        if abs(o - n) > 0.05 * o:
                            self.hung = True

    def get_feature_report(self, rid, size):
        with self.lock:
            if not self.unlocked:
                self.violations.append("read outside unlock/relock")
            _rid, addr, n = self.pending
            hdr = bytes([rid, 0x08, addr & 0xFF, ((addr >> 8) + 0x60) & 0xFF, n, 0x00, 0xFA, 0xFA])
            out = hdr + bytes(self.mem[addr:addr + n])
            return list(out + bytes(max(0, size - len(out))))

    def open_path(self, path): pass
    def close(self): pass
    def error(self): return "no error"

    def info(self):
        return {"path": self.PATH, "interface_number": 2, "usage_page": 0xFFA0, "usage": 1,
                "vendor_id": 0x04D9, "product_id": 0xFC30, "product_string": "Gaming Mouse",
                "manufacturer_string": "Holtek", "release_number": 0x0100}

    def dpi_record(self, p, i):
        base = (0x0042, 0x0102, 0x01B2, 0x0262, 0x0312)[p] + 2 + 6 * i
        return bytes(self.mem[base:base + 6])


def make_fake_hid(mouse: FakeMouse, present: bool = True):
    mod = types.ModuleType("hid")
    mod.device = lambda: mouse
    mod.enumerate = lambda vid=0, pid=0: ([mouse.info()] if present and (vid in (0, 0x04D9)) and pid in (0, 0xFC30) else [])
    return mod


class _Var:
    def __init__(self, master=None, value=None, name=None):
        self._v = self._default() if value is None else value
        self._cbs = []

    def _default(self):
        return None

    def get(self):
        return self._v

    def set(self, v):
        self._v = self._coerce(v)
        for cb in list(self._cbs):
            cb("v", "", "w")

    def _coerce(self, v):
        return v

    def trace_add(self, mode, cb):
        self._cbs.append(cb)
        return "trace%d" % len(self._cbs)


class StringVar(_Var):
    def _default(self): return ""
    def _coerce(self, v): return str(v)


class BooleanVar(_Var):
    def _default(self): return False
    def _coerce(self, v): return bool(v)


_F = {"width", "height", "corner_radius", "border_width", "bg_color", "fg_color", "border_color",
      "background_corner_colors", "overwrite_preferred_drawing_method", "cursor"}
ALLOWED = {
    "CTkFrame": _F,
    "CTkLabel": {"width", "height", "corner_radius", "bg_color", "fg_color", "text_color", "text_color_disabled",
                 "text", "font", "image", "compound", "anchor", "wraplength", "justify", "cursor", "padx", "pady",
                 "textvariable"},
    "CTkButton": {"width", "height", "corner_radius", "border_width", "border_spacing", "bg_color", "fg_color",
                  "hover_color", "border_color", "text_color", "text_color_disabled", "background_corner_colors",
                  "round_width_to_even_numbers", "text", "font", "textvariable", "image", "state", "hover",
                  "command", "compound", "anchor"},
    "CTkEntry": {"width", "height", "corner_radius", "border_width", "bg_color", "fg_color", "border_color",
                 "text_color", "placeholder_text_color", "textvariable", "placeholder_text", "font", "state",
                 "justify", "show"},
    "CTkSlider": {"width", "height", "corner_radius", "button_corner_radius", "border_width", "button_length",
                  "bg_color", "fg_color", "border_color", "progress_color", "button_color", "button_hover_color",
                  "from_", "to", "state", "number_of_steps", "hover", "command", "variable", "orientation"},
    "CTkSwitch": {"width", "height", "switch_width", "switch_height", "corner_radius", "border_width",
                  "button_length", "bg_color", "fg_color", "border_color", "progress_color", "button_color",
                  "button_hover_color", "text_color", "text_color_disabled", "text", "font", "textvariable",
                  "onvalue", "offvalue", "variable", "hover", "command", "state"},
    "CTkOptionMenu": {"width", "height", "corner_radius", "bg_color", "fg_color", "button_color",
                      "button_hover_color", "text_color", "text_color_disabled", "dropdown_fg_color",
                      "dropdown_hover_color", "dropdown_text_color", "font", "dropdown_font", "values",
                      "variable", "state", "hover", "command", "dynamic_resizing", "anchor"},
    "CTkTextbox": {"width", "height", "corner_radius", "border_width", "border_spacing", "bg_color", "fg_color",
                   "border_color", "text_color", "scrollbar_button_color", "scrollbar_button_hover_color", "font",
                   "activate_scrollbars", "wrap", "state"},
    "CTkProgressBar": {"width", "height", "corner_radius", "border_width", "bg_color", "fg_color", "border_color",
                       "progress_color", "variable", "orientation", "mode", "determinate_speed",
                       "indeterminate_speed"},
    "CTk": {"fg_color"},
    "Canvas": {"width", "height", "bg", "highlightthickness", "bd"},
}


class Widget:
    _kind = "Widget"

    def __init__(self, master=None, **kw):
        self._check(kw)
        self.master = master
        self.kw = dict(kw)
        self._binds = {}
        self.grid_state = None
        self.packed = False
        self.calls = []

    def _check(self, kw):
        allowed = ALLOWED.get(self._kind)
        if allowed is not None:
            bad = set(kw) - allowed
            if bad:
                raise TypeError("%s got unsupported argument(s): %s" % (self._kind, sorted(bad)))

    def configure(self, **kw):
        self._check(kw)
        self.kw.update(kw)

    def cget(self, k):
        return self.kw.get(k)

    def bind(self, seq, fn, add=None):
        self._binds.setdefault(seq, []).append(fn)

    def grid(self, **kw):
        assert not self.packed, "%s: grid() on a widget that was pack()ed" % self._kind
        self.grid_state = "on"

    def grid_remove(self):
        assert self.grid_state is not None, "grid_remove() before grid()"
        self.grid_state = "removed"

    def pack(self, **kw):
        assert self.grid_state is None, "%s: pack() on a widget that was grid()ed" % self._kind
        self.packed = True

    def grid_columnconfigure(self, i, **kw): pass
    def grid_rowconfigure(self, i, **kw): pass
    def grid_propagate(self, flag): pass
    def pack_propagate(self, flag): pass
    def tkraise(self): self.calls.append("tkraise")
    def winfo_width(self): return 800
    def winfo_height(self): return 400
    def destroy(self): self.calls.append("destroy")


def _mk(kind, extra=None, base=Widget):
    ns = {"_kind": kind}
    ns.update(extra or {})
    return type(kind, (base,), ns)


CTkFrame = _mk("CTkFrame")
CTkLabel = _mk("CTkLabel")


def _btn_invoke(self):
    if self.kw.get("state") == "disabled":
        return
    if self.kw.get("command"):
        self.kw["command"]()


CTkButton = _mk("CTkButton", {"invoke": _btn_invoke})


class _Slider(Widget):
    _kind = "CTkSlider"

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._val = kw.get("from_", 0)

    def set(self, v): self._val = v
    def get(self): return self._val


class _Entry(Widget):
    _kind = "CTkEntry"

    def get(self):
        v = self.kw.get("textvariable")
        return v.get() if v is not None else ""

    def insert(self, i, t): pass
    def delete(self, a, b=None): pass


class _Switch(Widget):
    _kind = "CTkSwitch"

    def toggle(self): self.kw["variable"].set(not self.kw["variable"].get())


class _Option(Widget):
    _kind = "CTkOptionMenu"

    def set(self, v): self.kw["variable"].set(v) if self.kw.get("variable") else None
    def get(self): return self.kw["variable"].get()


class _Text(Widget):
    _kind = "CTkTextbox"

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.text = ""

    def insert(self, idx, t):
        assert self.kw.get("state") != "disabled", "insert into a disabled textbox"
        self.text += t

    def get(self, a, b=None): return self.text
    def delete(self, a, b=None):
        assert self.kw.get("state") != "disabled", "delete in a disabled textbox"
        if a == "1.0" and b == "end":
            self.text = ""
        else:
            self.text = "\n".join(self.text.split("\n")[int(float(b)) - 1:])
    def index(self, i): return "%d.0" % (self.text.count("\n") + 1)
    def see(self, i): pass


class _Progress(Widget):
    _kind = "CTkProgressBar"

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.running = False

    def start(self): self.running = True
    def stop(self): self.running = False
    def set(self, v): pass


class _Canvas(Widget):
    _kind = "Canvas"

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items = []

    def delete(self, tag): self.items.clear()
    def create_line(self, *a, **k): self.items.append(("line", a, k))
    def create_oval(self, *a, **k): self.items.append(("oval", a, k))
    def create_text(self, *a, **k): self.items.append(("text", a, k))


class CTkFont:
    def __init__(self, family=None, size=None, weight=None, **kw):
        self.family, self.size, self.weight = family, size, weight


class CTkImage:
    def __init__(self, light_image=None, dark_image=None, size=None):
        self.size = size


class ScalingTracker:
    @staticmethod
    def get_window_scaling(w): return 1.0


class CTk(Widget):
    _kind = "CTk"
    LAST = None

    def __init__(self, **kw):
        super().__init__(None, **kw)
        self._afters = []
        self._protocols = {}
        self.icon = None
        self.destroyed = False
        self.clip = ""
        CTk.LAST = self

    def title(self, t): self.window_title = t
    def geometry(self, g): self.geo = g
    def minsize(self, w, h): self.min = (w, h)
    def iconbitmap(self, p): self.icon = p
    def iconphoto(self, flag, img): self.icon = img
    def protocol(self, name, fn): self._protocols[name] = fn
    def mainloop(self): pass
    def after(self, ms, fn=None, *a):
        self._afters.append((ms, fn))
        return "after#%d" % len(self._afters)
    def after_idle(self, fn):
        self._afters.append((0, fn))
        return "idle#%d" % len(self._afters)
    def clipboard_clear(self): self.clip = ""
    def clipboard_append(self, t): self.clip += t
    def destroy(self): self.destroyed = True

    def run_idle(self):
        """Run (and drop) the callbacks scheduled with after_idle / after(<=60 ms) - one pass."""
        pending, self._afters = self._afters, []
        keep = []
        for ms, fn in pending:
            if ms <= 60 and fn is not None:
                fn()
            else:
                keep.append((ms, fn))
        self._afters = keep + self._afters


class _Box:
    def __init__(self):
        self.calls, self.answer = [], True

    def showerror(self, title, msg, **k): self.calls.append(("error", msg))
    def showwarning(self, title, msg, **k): self.calls.append(("warning", msg))
    def askokcancel(self, title, msg, **k):
        self.calls.append(("askokcancel", msg))
        return self.answer


class _Files:
    def __init__(self): self.result = ""
    def askopenfilename(self, **k): return self.result


class _Colors:
    def __init__(self): self.result = (None, None)
    def askcolor(self, **k): return self.result


MESSAGEBOX, FILEDIALOG, COLORCHOOSER = _Box(), _Files(), _Colors()
_SAVED = {}


def install():
    ctk = types.ModuleType("customtkinter")
    for name, obj in dict(CTk=CTk, CTkFrame=CTkFrame, CTkLabel=CTkLabel, CTkButton=CTkButton, CTkEntry=_Entry,
                          CTkSlider=_Slider, CTkSwitch=_Switch, CTkOptionMenu=_Option, CTkTextbox=_Text,
                          CTkProgressBar=_Progress, CTkFont=CTkFont, CTkImage=CTkImage,
                          ScalingTracker=ScalingTracker, StringVar=StringVar, BooleanVar=BooleanVar).items():
        setattr(ctk, name, obj)
    ctk.set_appearance_mode = lambda m: None
    ctk.set_default_color_theme = lambda t: None
    tk = types.ModuleType("tkinter")
    tk.Canvas = _Canvas
    tk.PhotoImage = lambda **k: object()
    tk.messagebox, tk.filedialog, tk.colorchooser = MESSAGEBOX, FILEDIALOG, COLORCHOOSER
    for k in ("customtkinter", "tkinter", "tkinter.messagebox", "tkinter.filedialog", "tkinter.colorchooser"):
        _SAVED[k] = sys.modules.get(k)
    sys.modules.update({"customtkinter": ctk, "tkinter": tk, "tkinter.messagebox": MESSAGEBOX,
                        "tkinter.filedialog": FILEDIALOG, "tkinter.colorchooser": COLORCHOOSER})


def uninstall():
    for k, v in _SAVED.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
