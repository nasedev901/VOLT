# -*- coding: utf-8 -*-
"""
VOLT - GUI (customtkinter), dark only.

Layout:  sidebar (DPI / Lighting / Performance / Profiles / Advanced)
         header  (page title + connection badge)           <- fixed
         page area (all pages are built once and raised)
         action bar (status toast, unsaved / not-loaded pill, Restart mouse, Apply)  <- fixed

All protocol / restart / recovery logic lives in volt_backend.py and is only *called* from here.
"""
from __future__ import annotations

import colorsys
import datetime
import math
import os
import queue
import sys
import threading
import traceback
from typing import Callable, List, Optional

from volt_backend import (
    APP_NAME, AUTO_LABEL, BACKUP_DIR, BRIGHT_RANGE, DPI_MAX, DPI_MIN, DUMP_END, DUMP_START, IS_WINDOWS, MODES,
    NUM_PROFILES, NUM_STAGES, PID, SPEED_RANGE, VID, Config, DpiStage, LedSettings, LinkError,
    MouseLink, ProtocolError, RestartError, __version__, _path_text, apply_config, decode_dpi,
    decode_led, decode_poll, dpi_to_code, dump_memory, dump_to_text, endpoint_label, handshake,
    hex_to_rgb, hexs, is_admin, load_settings, normalize_hex, read_poll_raw, read_profile_raw,
    recover_led, relaunch_as_admin, resource_path, restart_mouse, restore_backup, rgb_to_hex,
    save_dump, save_settings, snap_dpi,
)

FAMILY = "Segoe UI" if IS_WINDOWS else "Helvetica"
MONO = "Consolas" if IS_WINDOWS else "Courier"


class C:
    """Palette: near-black surfaces, one green accent."""
    BG = "#0a0c0f"
    SIDEBAR = "#0e1114"
    SURFACE = "#13171b"
    SURFACE2 = "#1a1f25"
    HOVER = "#232a32"
    BORDER = "#242b33"
    TEXT = "#e9eef2"
    MUTED = "#8b97a4"
    FAINT = "#5d6874"
    ACCENT = "#2ee65a"
    ACCENT_HOVER = "#25c44b"
    ACCENT_DIM = "#10301b"
    ACCENT_MID = "#1c8a3b"
    ON_ACCENT = "#04130a"
    WARN = "#f2a93b"
    WARN_DIM = "#33260c"
    DANGER = "#ef5350"
    INFO = "#58a6ff"
    INFO_DIM = "#102640"
    SWITCH_OFF = "#2b323a"


PAGES = [
    ("dpi", "DPI", "Sensitivity stages"),
    ("light", "Lighting", "Effect, colour and brightness"),
    ("perf", "Performance", "Polling rate and how changes are applied"),
    ("profiles", "Profiles", "Profile selection and backups"),
    ("adv", "Advanced", "Device, tools and debug log"),
]
SWATCHES = ["#ff2d2d", "#ff8a00", "#ffd400", "#9dff2b", "#2ee65a", "#00e0b8",
            "#00c8ff", "#2d6bff", "#8a3dff", "#e02dff", "#ff4d94", "#ffffff"]
POLL_CHOICES = [(125, "8 ms"), (250, "4 ms"), (500, "2 ms"), (1000, "1 ms")]
BRIGHT_ITEMS = [(1, "Low"), (2, "Medium"), (3, "High")]
SPEED_ITEMS = [(1, "1"), (2, "2"), (3, "3"), (4, "4")]
DPI_TICKS = (100, 400, 800, 1600, 3200, 6400, 10000)
DEFAULT_STAGES = [400, 800, 1600, 3200, 6400]


def _hsv_hex(h: float, s: float = 1.0, v: float = 1.0) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return rgb_to_hex((int(r * 255), int(g * 255), int(b * 255)))


def _dim_hex(color: str, k: float) -> str:
    r, g, b = hex_to_rgb(color)
    k = max(0.0, min(1.0, k))
    return rgb_to_hex((int(r * k), int(g * k), int(b * k)))


def mode_preview_colors(mode: str, color: str, n: int = 12) -> List[str]:
    """LED-strip preview of a lighting mode: n colours for n dots."""
    off = "#2a3037"
    out: List[str] = []
    for i in range(n):
        t = i / (n - 1)
        if mode == "Static":
            out.append(color)
        elif mode == "Breathing":
            out.append(_dim_hex(color, 0.15 + 0.85 * (0.5 + 0.5 * math.cos(2 * math.pi * (t - 0.5)))))
        elif mode == "Rainbow":
            out.append(_hsv_hex(t * 0.85))
        elif mode == "Wave":
            out.append(_hsv_hex(0.55 + t * 0.35, 1.0, 0.55 + 0.45 * math.sin(2 * math.pi * t * 1.5)))
        elif mode == "Neon / Cycle":
            out.append(_hsv_hex(t * 0.85, 1.0, 0.3 + 0.7 * (0.5 + 0.5 * math.cos(2 * math.pi * (t - 0.5)))))
        elif mode == "Blink":
            out.append(color if (i // 2) % 2 == 0 else off)
        elif mode == "Reactive":
            out.append(_dim_hex(color, max(0.12, 1.0 - abs(t - 0.5) * 1.9)))
        else:
            out.append(off)
    return out


def write_crash_log(text: str) -> None:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(os.path.join(BACKUP_DIR, "volt-crash.log"), "a", encoding="utf-8") as fh:
            fh.write("--- %s ---\n%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), text))
    except OSError:
        pass


def run_gui(args) -> int:
    try:
        import customtkinter as ctk
    except ImportError:
        print("customtkinter is not installed.  Run:  py -m pip install customtkinter")
        return 2
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox
    try:
        from PIL import Image
    except Exception:
        Image = None

    log_q: "queue.Queue[str]" = queue.Queue()
    ui_q: "queue.Queue[Callable[[], None]]" = queue.Queue()

    def log(msg: str):
        stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        log_q.put("%s  %s" % (stamp, msg))

    link = MouseLink(log, dry_run=args.dry_run, interface_override=args.interface)
    F: dict = {}

    def make_fonts():
        def f(size, weight="normal"):
            return ctk.CTkFont(family=FAMILY, size=size, weight=weight)
        F.update(title=f(24, "bold"), h2=f(15, "bold"), h3=f(13, "bold"), body=f(13), body_b=f(13, "bold"),
                 small=f(11), nav=f(14, "bold"), big=f(20, "bold"), brand=f(20, "bold"),
                 mono=ctk.CTkFont(family=MONO, size=12))

    class Segmented(ctk.CTkFrame):
        """Small pill selector (selected item = accent fill, dark text)."""

        def __init__(self, master, items, command=None, height=36, min_width=58):
            super().__init__(master, fg_color=C.SURFACE2, corner_radius=10, border_width=1, border_color=C.BORDER)
            self._cmd = command
            self._value = None
            self._btn = {}
            last = len(items) - 1
            for n, (value, text) in enumerate(items):
                b = ctk.CTkButton(self, text=text, width=min_width, height=height - 10, corner_radius=7,
                                  fg_color="transparent", hover_color=C.HOVER, text_color=C.MUTED,
                                  text_color_disabled=C.FAINT, font=F["body"], border_width=0,
                                  command=lambda v=value: self._click(v))
                b.grid(row=0, column=n, padx=(4, 4 if n == last else 2), pady=4)
                self._btn[value] = b
            self.set(items[0][0])

        def _click(self, value):
            changed = value != self._value
            self.set(value)
            if changed and self._cmd:
                self._cmd(value)

        def set(self, value):
            self._value = value
            for v, b in self._btn.items():
                if v == value:
                    b.configure(fg_color=C.ACCENT, hover_color=C.ACCENT_HOVER, text_color=C.ON_ACCENT)
                else:
                    b.configure(fg_color="transparent", hover_color=C.HOVER, text_color=C.MUTED)

        def get(self):
            return self._value

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()
            make_fonts()
            self.title(APP_NAME)
            self.configure(fg_color=C.BG)
            self.geometry("1060x740")
            self.minsize(960, 680)
            self._set_icon()

            self.busy = False
            self.connected = False
            self.write_ok = False
            self._last_present = None
            self.pending_reload = False
            self._loading = False
            self._toast_id = 0
            self._ladder_job = None
            self._style_cache = {}
            self._action_buttons: list = []
            self.endpoint_infos: list = []
            self.mode = "Static"
            self._color = "#ff2d2d"
            self.poll_hz = 1000
            self.stage_dpi = list(DEFAULT_STAGES)
            self.page = None
            self.s = self._ui_scale()

            self._build()
            self._baseline = self._snapshot()
            self._show_page("dpi")
            self._refresh_state()
            self._refresh_endpoints()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.bind("<Control-s>", lambda _e: self._on_apply())
            self.after(50, self._pump)
            self.after(300, self._poll_presence)

        def _ui_scale(self) -> float:
            try:
                return float(ctk.ScalingTracker.get_window_scaling(self))
            except Exception:
                return 1.0

        def _set_icon(self):
            """Window + taskbar icon. Never fatal: a missing/bad icon is silently skipped."""
            ico = resource_path("assets/volt.ico")
            png = resource_path("assets/icon.png")
            try:
                if IS_WINDOWS and os.path.isfile(ico):
                    self.iconbitmap(ico)
                    self.after(300, lambda: self._safe_iconbitmap(ico))
                elif os.path.isfile(png):
                    self._icon_img = tk.PhotoImage(file=png)
                    self.iconphoto(True, self._icon_img)
            except Exception as exc:
                log("icon not set: %r" % (exc,))

        def _safe_iconbitmap(self, path):
            try:
                self.iconbitmap(path)
            except Exception:
                pass

        def report_callback_exception(self, exc, val, tb):
            text = "".join(traceback.format_exception(exc, val, tb))
            log("UI ERROR:\n" + text)
            write_crash_log(text)
            try:
                self._toast("Internal error - details in Advanced > debug log.", "err")
            except Exception:
                pass

        def _card(self, parent, radius=14):
            return ctk.CTkFrame(parent, fg_color=C.SURFACE, corner_radius=radius, border_width=1,
                                border_color=C.BORDER)

        def _h2(self, parent, text, sub=None):
            ctk.CTkLabel(parent, text=text, font=F["h2"], text_color=C.TEXT, anchor="w").pack(
                fill="x", padx=20, pady=(16, 0 if sub else 8))
            if sub:
                ctk.CTkLabel(parent, text=sub, font=F["small"], text_color=C.MUTED, anchor="w",
                             justify="left", wraplength=620).pack(fill="x", padx=20, pady=(2, 10))

        def _note(self, parent, text, pady=(0, 14), wrap=620):
            ctk.CTkLabel(parent, text=text, font=F["small"], text_color=C.MUTED, anchor="w", justify="left",
                         wraplength=wrap).pack(fill="x", padx=20, pady=pady)

        def _switch(self, parent, text, var, command=None):
            return ctk.CTkSwitch(parent, text=text, variable=var, font=F["body"], text_color=C.TEXT,
                                 progress_color=C.ACCENT, fg_color=C.SWITCH_OFF, button_color="#f2f5f7",
                                 button_hover_color="#ffffff", switch_width=38, switch_height=20,
                                 command=command)

        def _ghost_btn(self, parent, text, command, width=120):
            b = ctk.CTkButton(parent, text=text, width=width, height=34, corner_radius=9, font=F["body"],
                              fg_color=C.SURFACE2, hover_color=C.HOVER, text_color=C.TEXT,
                              text_color_disabled=C.FAINT, border_width=1, border_color=C.BORDER,
                              command=command)
            self._action_buttons.append(b)
            return b

        def _build(self):
            self.grid_columnconfigure(2, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._build_sidebar()
            ctk.CTkFrame(self, width=1, fg_color=C.BORDER, corner_radius=0).grid(row=0, column=1, sticky="ns")
            main = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
            main.grid(row=0, column=2, sticky="nsew")
            main.grid_columnconfigure(0, weight=1)
            main.grid_rowconfigure(2, weight=1)
            self._build_header(main)
            self._build_banner(main)
            self.pages = ctk.CTkFrame(main, fg_color="transparent", corner_radius=0)
            self.pages.grid(row=2, column=0, sticky="nsew", padx=28, pady=(4, 12))
            self.pages.grid_columnconfigure(0, weight=1)
            self.pages.grid_rowconfigure(0, weight=1)
            self.page_frames = {}
            for key, _t, _s in PAGES:
                fr = ctk.CTkFrame(self.pages, fg_color="transparent", corner_radius=0)
                fr.grid(row=0, column=0, sticky="nsew")
                fr.grid_columnconfigure(0, weight=1)
                self.page_frames[key] = fr
            self._build_dpi(self.page_frames["dpi"])
            self._build_light(self.page_frames["light"])
            self._build_perf(self.page_frames["perf"])
            self._build_profiles(self.page_frames["profiles"])
            self._build_adv(self.page_frames["adv"])
            self._build_bar(main)

        def _build_sidebar(self):
            sb = ctk.CTkFrame(self, width=208, corner_radius=0, fg_color=C.SIDEBAR)
            sb.grid(row=0, column=0, sticky="nsew")
            sb.pack_propagate(False)
            brand = ctk.CTkFrame(sb, fg_color="transparent")
            brand.pack(fill="x", padx=18, pady=(22, 22))
            logo = None
            if Image is not None:
                try:
                    with Image.open(resource_path("assets/logo.png")) as im:
                        img = im.copy()
                    logo = ctk.CTkImage(light_image=img, dark_image=img, size=(38, 38))
                except Exception as exc:
                    log("logo not loaded: %r" % (exc,))
            if logo is not None:
                self._logo_img = logo
                ctk.CTkLabel(brand, text="", image=logo).pack(side="left")
            titles = ctk.CTkFrame(brand, fg_color="transparent")
            titles.pack(side="left", padx=(10, 0))
            ctk.CTkLabel(titles, text="VOLT", font=F["brand"], text_color=C.TEXT, anchor="w").pack(anchor="w")
            ctk.CTkLabel(titles, text="Redragon M711", font=F["small"], text_color=C.MUTED, anchor="w").pack(anchor="w")
            self.nav = {}
            for key, label, _sub in PAGES:
                b = ctk.CTkButton(sb, text=label, anchor="w", height=40, corner_radius=10, border_spacing=16,
                                  fg_color="transparent", hover_color=C.SURFACE2, text_color=C.MUTED,
                                  font=F["nav"], command=lambda k=key: self._show_page(k))
                b.pack(fill="x", padx=12, pady=2)
                self.nav[key] = b
            foot = ctk.CTkLabel(sb, text="v%s%s" % (__version__, "   DRY RUN" if link.dry_run else ""),
                                font=F["small"], text_color=C.INFO if link.dry_run else C.FAINT, anchor="w")
            foot.pack(side="bottom", fill="x", padx=22, pady=16)

        def _show_page(self, key):
            self.page = key
            self.page_frames[key].tkraise()
            for k, b in self.nav.items():
                if k == key:
                    b.configure(fg_color=C.ACCENT_DIM, text_color=C.ACCENT, hover_color=C.ACCENT_DIM)
                else:
                    b.configure(fg_color="transparent", text_color=C.MUTED, hover_color=C.SURFACE2)
            for k, t, sub in PAGES:
                if k == key:
                    self.page_title.configure(text=t)
                    self.page_sub.configure(text=sub)

        def _build_header(self, main):
            hdr = ctk.CTkFrame(main, fg_color="transparent")
            hdr.grid(row=0, column=0, sticky="ew", padx=28, pady=(22, 6))
            hdr.grid_columnconfigure(0, weight=1)
            left = ctk.CTkFrame(hdr, fg_color="transparent")
            left.grid(row=0, column=0, sticky="w")
            self.page_title = ctk.CTkLabel(left, text="", font=F["title"], text_color=C.TEXT, anchor="w")
            self.page_title.pack(anchor="w")
            self.page_sub = ctk.CTkLabel(left, text="", font=F["small"], text_color=C.MUTED, anchor="w")
            self.page_sub.pack(anchor="w")
            right = ctk.CTkFrame(hdr, fg_color="transparent")
            right.grid(row=0, column=1, sticky="e")
            self.conn_pill = ctk.CTkFrame(right, fg_color=C.SURFACE, corner_radius=15, border_width=1,
                                          border_color=C.BORDER, height=30)
            self.conn_pill.pack(side="left", padx=(0, 8))
            self.conn_dot = ctk.CTkLabel(self.conn_pill, text="\u25CF", font=F["small"], text_color=C.DANGER, width=10)
            self.conn_dot.pack(side="left", padx=(12, 4), pady=4)
            self.conn_text = ctk.CTkLabel(self.conn_pill, text="Not connected", font=F["body"], text_color=C.TEXT)
            self.conn_text.pack(side="left", padx=(0, 14), pady=4)
            self.rescan_btn = ctk.CTkButton(right, text="Rescan", width=70, height=30, corner_radius=9,
                                            font=F["small"], fg_color="transparent", hover_color=C.SURFACE2,
                                            text_color=C.MUTED, text_color_disabled=C.FAINT,
                                            border_width=1, border_color=C.BORDER, command=self._rescan)
            self.rescan_btn.pack(side="left")
            self._action_buttons.append(self.rescan_btn)

        def _build_banner(self, main):
            self.banner = ctk.CTkFrame(main, fg_color=C.WARN_DIM, corner_radius=12, border_width=1,
                                       border_color=C.WARN)
            self.banner.grid(row=1, column=0, sticky="ew", padx=28, pady=(6, 2))
            self.banner.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(self.banner, anchor="w", justify="left", font=F["small"], text_color=C.WARN, wraplength=520,
                         text="Not running as Administrator. Settings can still be saved, but Windows will not let "
                              "VOLT restart the mouse to load them.").grid(row=0, column=0, sticky="w", padx=14, pady=8)
            ctk.CTkButton(self.banner, text="Relaunch as admin", width=130, height=28, corner_radius=8,
                          font=F["small"], fg_color=C.WARN, hover_color="#d8932d", text_color="#1a1203",
                          command=self._on_relaunch_admin).grid(row=0, column=1, padx=(0, 6), pady=8)
            ctk.CTkButton(self.banner, text="\u2715", width=28, height=28, corner_radius=8, font=F["small"],
                          fg_color="transparent", hover_color=C.WARN_DIM, text_color=C.WARN,
                          command=lambda: self.banner.grid_remove()).grid(row=0, column=2, padx=(0, 8), pady=8)
            if not IS_WINDOWS or is_admin():
                self.banner.grid_remove()

        def _on_relaunch_admin(self):
            if relaunch_as_admin():
                self._on_close()
            else:
                self._toast("Could not relaunch as Administrator (cancelled?).", "warn")

        def _build_bar(self, main):
            self.progress = ctk.CTkProgressBar(main, height=3, corner_radius=0, fg_color=C.BORDER,
                                               progress_color=C.ACCENT, mode="determinate")
            self.progress.grid(row=3, column=0, sticky="ew")
            self.progress.set(0)
            bar = ctk.CTkFrame(main, fg_color=C.SIDEBAR, corner_radius=0, height=74)
            bar.grid(row=4, column=0, sticky="ew")
            bar.grid_propagate(False)
            bar.grid_columnconfigure(0, weight=1)
            bar.grid_rowconfigure(0, weight=1)
            toast = ctk.CTkFrame(bar, fg_color="transparent")
            toast.grid(row=0, column=0, sticky="w", padx=(28, 8))
            self.toast_dot = ctk.CTkLabel(toast, text="", font=F["small"], text_color=C.MUTED, width=12)
            self.toast_dot.pack(side="left", padx=(0, 6))
            self.toast_msg = ctk.CTkLabel(toast, text="", font=F["body"], text_color=C.TEXT, anchor="w",
                                          justify="left", wraplength=300)
            self.toast_msg.pack(side="left")
            self.state_pill = ctk.CTkFrame(bar, fg_color=C.WARN_DIM, corner_radius=14, height=28)
            self.state_pill.grid(row=0, column=1, padx=(0, 14))
            self.state_dot = ctk.CTkLabel(self.state_pill, text="\u25CF", font=F["small"], text_color=C.WARN, width=10)
            self.state_dot.pack(side="left", padx=(12, 4), pady=3)
            self.state_text = ctk.CTkLabel(self.state_pill, text="", font=F["small"], text_color=C.WARN)
            self.state_text.pack(side="left", padx=(0, 12), pady=3)
            self.restart_btn = ctk.CTkButton(bar, text="Restart mouse", width=132, height=40, corner_radius=10,
                                             font=F["body_b"], fg_color=C.SURFACE2, hover_color=C.HOVER,
                                             text_color=C.TEXT, text_color_disabled=C.FAINT, border_width=1,
                                             border_color=C.BORDER, command=self._on_restart_now)
            self.restart_btn.grid(row=0, column=2, padx=(0, 10))
            self.apply_btn = ctk.CTkButton(bar, text="Apply", width=118, height=40, corner_radius=10,
                                           font=F["body_b"], fg_color=C.SURFACE2, hover_color=C.HOVER,
                                           text_color=C.TEXT, text_color_disabled=C.FAINT, command=self._on_apply)
            self.apply_btn.grid(row=0, column=3, padx=(0, 28))
            self._action_buttons += [self.restart_btn, self.apply_btn]

        def _build_dpi(self, page):
            card = self._card(page)
            card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            self.ladder = tk.Canvas(card, height=int(92 * self.s), bg=C.SURFACE, highlightthickness=0, bd=0)
            self.ladder.pack(fill="x", padx=16, pady=10)
            self.ladder.bind("<Configure>", lambda _e: self._schedule_ladder())
            self.stage_enabled, self.stage_slider, self.stage_var = [], [], []
            self.stage_eff, self.stage_title, self.stage_entry = [], [], []
            for i in range(NUM_STAGES):
                row = self._card(page, radius=12)
                row.grid(row=i + 1, column=0, sticky="ew", pady=3)
                row.grid_columnconfigure(2, weight=1)
                row.grid_columnconfigure(1, minsize=132)
                en = ctk.BooleanVar(value=True)
                en.trace_add("write", lambda *_a, i=i: self._on_stage_var(i))
                ctk.CTkSwitch(row, text="", variable=en, width=46, switch_width=38, switch_height=20,
                              progress_color=C.ACCENT, fg_color=C.SWITCH_OFF, button_color="#f2f5f7",
                              button_hover_color="#ffffff").grid(row=0, column=0, rowspan=2, padx=(18, 4), pady=10)
                ttl = ctk.CTkLabel(row, text="Stage %d" % (i + 1), font=F["h3"], text_color=C.TEXT, anchor="w")
                ttl.grid(row=0, column=1, sticky="sw", padx=(10, 0), pady=(10, 0))
                eff = ctk.CTkLabel(row, text="", font=F["small"], text_color=C.FAINT, anchor="w")
                eff.grid(row=1, column=1, sticky="nw", padx=(10, 0), pady=(0, 10))
                sl = ctk.CTkSlider(row, from_=DPI_MIN, to=DPI_MAX, number_of_steps=(DPI_MAX - DPI_MIN) // 100,
                                   progress_color=C.ACCENT, button_color=C.ACCENT, button_hover_color=C.ACCENT_HOVER,
                                   fg_color=C.SWITCH_OFF, command=lambda v, i=i: self._on_slider(i, v))
                sl.set(DEFAULT_STAGES[i])
                sl.grid(row=0, column=2, rowspan=2, sticky="ew", padx=16)
                var = ctk.StringVar(value=str(DEFAULT_STAGES[i]))
                ent = ctk.CTkEntry(row, textvariable=var, width=76, height=34, justify="center", font=F["body_b"],
                                   fg_color=C.SURFACE2, border_color=C.BORDER, border_width=1, text_color=C.TEXT,
                                   corner_radius=8)
                ent.grid(row=0, column=3, rowspan=2, padx=(0, 6))
                ent.bind("<Return>", lambda _e, i=i: self._on_entry(i))
                ent.bind("<FocusOut>", lambda _e, i=i: self._on_entry(i))
                ent.bind("<KeyRelease>", lambda _e, i=i: self._on_entry_key(i))
                ctk.CTkLabel(row, text="DPI", font=F["small"], text_color=C.MUTED).grid(
                    row=0, column=4, rowspan=2, padx=(0, 18))
                self.stage_enabled.append(en)
                self.stage_slider.append(sl)
                self.stage_var.append(var)
                self.stage_eff.append(eff)
                self.stage_title.append(ttl)
                self.stage_entry.append(ent)
                self._update_eff(i)
            ctk.CTkLabel(page, anchor="w", justify="left", font=F["small"], text_color=C.FAINT, wraplength=700,
                         text="The firmware stores DPI in steps of about 44.4 (x2 above ~4900), so 1300 is saved as "
                              "1289 - 'stored' is what the mouse really uses. The DPI button on the mouse cycles the "
                              "enabled stages; at least one must stay on. A DPI change restarts the mouse's USB "
                              "device (~2 s).").grid(row=NUM_STAGES + 1, column=0, sticky="w", pady=(8, 0))

        def _stage_value(self, i):
            try:
                return snap_dpi(int(float(self.stage_var[i].get().strip())))
            except (ValueError, AttributeError):
                return self.stage_dpi[i]

        def _update_eff(self, i):
            v = self._stage_value(i)
            _lo, _hi, eff = dpi_to_code(v)
            self.stage_eff[i].configure(text="stored: %d DPI" % eff, text_color=C.FAINT if eff == v else C.MUTED)

        def _apply_stage_look(self, i):
            on = bool(self.stage_enabled[i].get())
            self.stage_title[i].configure(text_color=C.TEXT if on else C.FAINT)
            self.stage_slider[i].configure(state="normal" if on else "disabled",
                                           progress_color=C.ACCENT if on else C.FAINT,
                                           button_color=C.ACCENT if on else C.FAINT,
                                           button_hover_color=C.ACCENT_HOVER if on else C.FAINT)
            self.stage_entry[i].configure(state="normal" if on else "disabled",
                                          text_color=C.TEXT if on else C.FAINT)

        def _on_stage_var(self, i):
            if not self._loading and not any(v.get() for v in self.stage_enabled):
                self.stage_enabled[i].set(True)
                self._toast("At least one DPI stage must stay enabled.", "warn")
                return
            self._apply_stage_look(i)
            self._schedule_ladder()
            self._changed()

        def _on_slider(self, i, value):
            v = snap_dpi(value)
            self.stage_dpi[i] = v
            self.stage_var[i].set(str(v))
            self._update_eff(i)
            self._schedule_ladder()
            self._changed()

        def _on_entry_key(self, i):
            self._update_eff(i)
            self._schedule_ladder()
            self._changed()

        def _on_entry(self, i):
            try:
                v = snap_dpi(int(float(self.stage_var[i].get().strip())))
            except ValueError:
                v = self.stage_dpi[i]
            self.stage_dpi[i] = v
            self.stage_var[i].set(str(v))
            self.stage_slider[i].set(v)
            self._update_eff(i)
            self._schedule_ladder()
            self._changed()

        def _schedule_ladder(self):
            if self._ladder_job is None:
                self._ladder_job = self.after_idle(self._ladder_job_run)

        def _ladder_job_run(self):
            self._ladder_job = None
            self._draw_ladder()

        def _draw_ladder(self):
            cv, s = self.ladder, self.s
            w = int(cv.winfo_width())
            if w < 120:
                return
            cv.delete("all")
            x0, x1, y = 34 * s, w - 34 * s, 54 * s

            def X(v):
                return x0 + (math.log10(max(100, min(10000, v))) - 2.0) / 2.0 * (x1 - x0)

            cv.create_line(x0, y, x1, y, width=max(2, int(4 * s)), fill=C.BORDER, capstyle="round")
            for t in DPI_TICKS:
                cv.create_line(X(t), y + 8 * s, X(t), y + 13 * s, fill=C.FAINT)
                cv.create_text(X(t), y + 25 * s, text="10k" if t == 10000 else str(t), fill=C.FAINT,
                               font=(FAMILY, 8))
            stages = [(i, self._stage_value(i), bool(self.stage_enabled[i].get())) for i in range(NUM_STAGES)]
            on = [X(v) for _i, v, e in stages if e]
            if len(on) >= 2:
                cv.create_line(min(on), y, max(on), y, width=max(2, int(4 * s)), fill=C.ACCENT_MID, capstyle="round")
            last = [-1e9, -1e9]
            r = 10 * s
            for i, v, e in sorted(stages, key=lambda t: t[1]):
                x = X(v)
                row = 0 if x - last[0] >= 50 * s else (1 if x - last[1] >= 50 * s else 0)
                last[row] = x
                cv.create_oval(x - r, y - r, x + r, y + r, fill=C.ACCENT if e else C.SURFACE2,
                               outline="" if e else C.FAINT, width=1)
                cv.create_text(x, y, text=str(i + 1), fill=C.ON_ACCENT if e else C.FAINT, font=(FAMILY, 9, "bold"))
                cv.create_text(x, y - (24 + 16 * row) * s, text=str(v), fill=C.TEXT if e else C.FAINT,
                               font=(FAMILY, 10, "bold"))

        def _build_light(self, page):
            page.grid_columnconfigure(0, weight=3)
            page.grid_columnconfigure(1, weight=2)
            card = self._card(page)
            card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
            self._h2(card, "Effect")
            grid = ctk.CTkFrame(card, fg_color="transparent")
            grid.pack(fill="x", padx=14, pady=(0, 14))
            self.mode_tiles = {}
            names = list(MODES.keys())
            for col in range(4):
                grid.grid_columnconfigure(col, weight=1, uniform="modes")
            for n, name in enumerate(names):
                self._build_mode_tile(grid, name, n // 4, n % 4)

            self.col_card = self._card(page)
            self.col_card.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
            self.col_card.configure(height=int(232 * self.s))
            self.col_card.grid_propagate(False)
            self.col_card.grid_columnconfigure(0, weight=1)
            self.col_card.grid_rowconfigure(1, weight=1)
            ctk.CTkLabel(self.col_card, text="Colour", font=F["h2"], text_color=C.TEXT, anchor="w").grid(
                row=0, column=0, sticky="w", padx=20, pady=(16, 4))
            self.col_body = ctk.CTkFrame(self.col_card, fg_color="transparent")
            self.col_body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 14))
            self.col_note = ctk.CTkLabel(self.col_card, text="This effect controls its own colours.\nNothing to pick here.",
                                         font=F["body"], text_color=C.MUTED, justify="center")
            self.col_note.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 14))
            top = ctk.CTkFrame(self.col_body, fg_color="transparent")
            top.pack(fill="x", pady=(0, 12))
            self.hex_var = ctk.StringVar(value=self._color)
            self.preview_swatch = ctk.CTkFrame(top, width=46, height=46, corner_radius=12, fg_color=self._color,
                                               border_width=1, border_color=C.BORDER)
            self.preview_swatch.pack(side="left")
            self.hex_entry = ctk.CTkEntry(top, textvariable=self.hex_var, width=104, height=36, font=F["mono"],
                                          fg_color=C.SURFACE2, border_color=C.BORDER, border_width=1,
                                          text_color=C.TEXT, corner_radius=8)
            self.hex_entry.pack(side="left", padx=(12, 8))
            self.hex_entry.bind("<Return>", lambda _e: self._on_hex())
            self.hex_entry.bind("<FocusOut>", lambda _e: self._on_hex())
            self.hex_entry.bind("<KeyRelease>", lambda _e: self._on_hex_key())
            ctk.CTkButton(top, text="Custom\u2026", width=84, height=36, corner_radius=8, font=F["body"],
                          fg_color=C.SURFACE2, hover_color=C.HOVER, text_color=C.TEXT, border_width=1,
                          border_color=C.BORDER, command=self._pick_color).pack(side="left")
            sw = ctk.CTkFrame(self.col_body, fg_color="transparent")
            sw.pack(fill="x")
            self.swatch_btns = {}
            for n, col in enumerate(SWATCHES):
                b = ctk.CTkButton(sw, text="", width=28, height=28, corner_radius=14, fg_color=col,
                                  hover_color=col, border_width=2, border_color=C.SURFACE,
                                  command=lambda c=col: self._set_color(c))
                b.grid(row=n // 6, column=n % 6, padx=4, pady=4)
                self.swatch_btns[col] = b

            side = self._card(page)
            side.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
            side.configure(height=int(232 * self.s))
            side.grid_propagate(False)
            side.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(side, text="Brightness", font=F["h3"], text_color=C.TEXT, anchor="w").grid(
                row=0, column=0, sticky="w", padx=20, pady=(16, 6))
            self.bright = Segmented(side, BRIGHT_ITEMS, command=lambda _v: self._changed(), min_width=66)
            self.bright.set(BRIGHT_RANGE[1])
            self.bright.grid(row=1, column=0, sticky="w", padx=20)
            ctk.CTkLabel(side, text="Animation speed", font=F["h3"], text_color=C.TEXT, anchor="w").grid(
                row=2, column=0, sticky="w", padx=20, pady=(18, 6))
            self.speed = Segmented(side, SPEED_ITEMS, command=lambda _v: self._changed(), min_width=46)
            self.speed.set(2)
            self.speed.grid(row=3, column=0, sticky="w", padx=20)
            ctk.CTkLabel(side, text="slow  \u2192  fast", font=F["small"], text_color=C.FAINT, anchor="w").grid(
                row=4, column=0, sticky="w", padx=22, pady=(4, 0))
            self._select_mode("Static", user=False)

        def _build_mode_tile(self, parent, name, row, col):
            tile = ctk.CTkFrame(parent, fg_color=C.SURFACE2, corner_radius=12, border_width=2, border_color=C.BORDER)
            tile.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
            cv = tk.Canvas(tile, width=int(132 * self.s), height=int(22 * self.s), bg=C.SURFACE2,
                           highlightthickness=0, bd=0)
            cv.pack(padx=10, pady=(14, 6))
            lbl = ctk.CTkLabel(tile, text=name, font=F["body_b"], text_color=C.TEXT)
            lbl.pack(pady=(0, 12))
            for w in (tile, cv, lbl):
                w.bind("<Button-1>", lambda _e, n=name: self._select_mode(n))
            try:
                tile.configure(cursor="hand2")
            except Exception:
                pass
            self.mode_tiles[name] = (tile, cv)
            self._paint_tile(name)

        def _paint_tile(self, name):
            tile, cv = self.mode_tiles[name]
            sel = name == self.mode
            bg = C.ACCENT_DIM if sel else C.SURFACE2
            tile.configure(fg_color=bg, border_color=C.ACCENT if sel else C.BORDER)
            cv.configure(bg=bg)
            cv.delete("all")
            n, s = 12, self.s
            width, r = 132 * s, 4.5 * s
            step = (width - 2 * r) / (n - 1)
            for k, col in enumerate(mode_preview_colors(name, self._color, n)):
                cx = r + k * step
                cv.create_oval(cx - r, 11 * s - r, cx + r, 11 * s + r, fill=col, outline="")

        def _select_mode(self, name, user=True):
            if name not in MODES:
                return
            prev = self.mode
            self.mode = name
            if prev in self.mode_tiles:
                self._paint_tile(prev)
            self._paint_tile(name)
            if MODES[name][2]:
                self.col_note.grid_remove()
                self.col_body.grid()
            else:
                self.col_body.grid_remove()
                self.col_note.grid()
            if user:
                self._changed()

        def _set_color(self, hex_text, user=True, from_entry=False):
            h = normalize_hex(hex_text)
            if not h:
                return
            self._color = h
            if not from_entry:
                self.hex_var.set(h)
            self.preview_swatch.configure(fg_color=h)
            for col, b in self.swatch_btns.items():
                b.configure(border_color=C.TEXT if col.lower() == h else C.SURFACE)
            for name in MODES:
                if MODES[name][2]:
                    self._paint_tile(name)
            if user:
                self._changed()

        def _on_hex_key(self):
            text = self.hex_var.get().strip().lstrip("#")
            if len(text) == 6 and normalize_hex(text):
                self._set_color(text, from_entry=True)

        def _on_hex(self):
            h = normalize_hex(self.hex_var.get())
            if h:
                self._set_color(h)
            else:
                self.hex_var.set(self._color)

        def _pick_color(self):
            _rgb, hexv = colorchooser.askcolor(color=self._color, parent=self, title="LED colour")
            if hexv:
                self._set_color(hexv)

        def _build_perf(self, page):
            card = self._card(page)
            card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            self._h2(card, "Polling rate", "How often the mouse reports its position. Higher = smoother and lower latency.")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(0, 16))
            self.poll_btns = {}
            for n, (hz, ms) in enumerate(POLL_CHOICES):
                row.grid_columnconfigure(n, weight=1, uniform="poll")
                cell = ctk.CTkFrame(row, fg_color="transparent")
                cell.grid(row=0, column=n, padx=6, sticky="nsew")
                b = ctk.CTkButton(cell, text="%d Hz" % hz, height=64, corner_radius=12, font=F["big"],
                                  fg_color=C.SURFACE2, hover_color=C.HOVER, text_color=C.MUTED,
                                  border_width=2, border_color=C.BORDER, command=lambda h=hz: self._set_poll(h))
                b.pack(fill="x")
                ctk.CTkLabel(cell, text="%s response" % ms, font=F["small"], text_color=C.FAINT).pack(pady=(6, 0))
                self.poll_btns[hz] = b
            self._set_poll(1000, user=False)

            card = self._card(page)
            card.grid(row=1, column=0, sticky="ew")
            self._h2(card, "Applying changes")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 4))
            self.autorestart_var = ctk.BooleanVar(value=bool(load_settings().get("auto_restart", False)))
            self._switch(row, "Also restart the mouse after colour / polling changes", self.autorestart_var,
                         command=self._on_autorestart_toggle).pack(anchor="w")
            self._note(card, "The mouse only loads saved settings at power-up. A changed DPI always restarts the "
                             "mouse's USB device (the cursor freezes for about 2 seconds). Colour and polling "
                             "changes are just saved until you press 'Restart mouse' - or switch this on to restart "
                             "on every Apply. Restarting needs Administrator rights.", pady=(10, 16))

        def _set_poll(self, hz, user=True):
            self.poll_hz = hz
            for h, b in self.poll_btns.items():
                if h == hz:
                    b.configure(fg_color=C.ACCENT_DIM, border_color=C.ACCENT, text_color=C.ACCENT, hover_color=C.ACCENT_DIM)
                else:
                    b.configure(fg_color=C.SURFACE2, border_color=C.BORDER, text_color=C.MUTED, hover_color=C.HOVER)
            if user:
                self._changed()

        def _on_autorestart_toggle(self):
            save_settings({**load_settings(), "auto_restart": bool(self.autorestart_var.get())})

        def _build_profiles(self, page):
            card = self._card(page)
            card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            self._h2(card, "Profile", "The M711 stores 5 independent profiles (DPI, lighting, polling).")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 6))
            ctk.CTkLabel(row, text="Editing", font=F["body"], text_color=C.MUTED).pack(side="left", padx=(0, 10))
            self.profile_sel = Segmented(row, [(n, str(n)) for n in range(1, NUM_PROFILES + 1)],
                                         command=self._on_profile_change, min_width=44)
            self.profile_sel.pack(side="left")
            self.active_label = ctk.CTkLabel(row, text="Mouse active profile byte: ?", font=F["small"],
                                             text_color=C.FAINT)
            self.active_label.pack(side="right")
            row2 = ctk.CTkFrame(card, fg_color="transparent")
            row2.pack(fill="x", padx=20, pady=(8, 0))
            self.all_profiles = ctk.BooleanVar(value=True)
            self._switch(row2, "Apply to all 5 profiles", self.all_profiles).pack(anchor="w")
            self._note(card, "Whether the mouse counts its active profile from 0 or 1 is not verified, which is why "
                             "'Apply to all 5 profiles' is on by default. Switching the profile above loads its "
                             "values from the mouse.", pady=(10, 16))

            card = self._card(page)
            card.grid(row=1, column=0, sticky="ew")
            self._h2(card, "Backups", "A backup of the mouse's profiles is saved automatically before every Apply.")
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 6))
            self.restore_btn = self._ghost_btn(row, "Restore backup\u2026", self._on_restore, width=150)
            self.restore_btn.pack(side="left")
            self._ghost_btn(row, "Open backup folder", self._open_backups, width=160).pack(side="left", padx=8)
            self.read_btn = self._ghost_btn(row, "Read from mouse", self._on_read, width=150)
            self.read_btn.pack(side="left")
            self._note(card, BACKUP_DIR, pady=(6, 16))

        def _open_backups(self):
            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                os.startfile(BACKUP_DIR)
            except Exception as exc:
                self._toast("Could not open the folder: %s" % exc, "warn")

        def _build_adv(self, page):
            page.grid_columnconfigure(0, weight=1)
            page.grid_columnconfigure(1, weight=1)
            page.grid_rowconfigure(1, weight=1)
            left = ctk.CTkFrame(page, fg_color="transparent")
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            left.grid_columnconfigure(0, weight=1)
            dev = self._card(left)
            dev.grid(row=0, column=0, sticky="ew")
            self._h2(dev, "HID endpoint")
            self.dev_label = ctk.CTkLabel(dev, text="04D9:FC30  -  looking for mouse...", font=F["small"],
                                          text_color=C.MUTED, anchor="w", justify="left", wraplength=300)
            self.dev_label.pack(fill="x", padx=20, pady=(0, 8))
            row = ctk.CTkFrame(dev, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 6))
            self.endpoint_var = ctk.StringVar(value=AUTO_LABEL)
            self.endpoint_menu = ctk.CTkOptionMenu(row, values=[AUTO_LABEL], variable=self.endpoint_var, width=206,
                                                   height=32, dynamic_resizing=False, font=F["small"],
                                                   dropdown_font=F["small"], fg_color=C.SURFACE2,
                                                   button_color=C.HOVER, button_hover_color=C.BORDER,
                                                   text_color=C.TEXT, dropdown_fg_color=C.SURFACE2,
                                                   dropdown_hover_color=C.HOVER, dropdown_text_color=C.TEXT,
                                                   command=self._on_endpoint_change)
            self.endpoint_menu.pack(side="left")
            self.endpoint_btn = self._ghost_btn(row, "Refresh", lambda: self._refresh_endpoints(show_details=True), 66)
            self.endpoint_btn.configure(height=32)
            self.endpoint_btn.pack(side="left", padx=(8, 0))
            self.broadcast_var = ctk.BooleanVar(value=False)
            r2 = ctk.CTkFrame(dev, fg_color="transparent")
            r2.pack(fill="x", padx=20, pady=(4, 14))
            self._switch(r2, "Broadcast Apply to every vendor endpoint", self.broadcast_var).pack(anchor="w")

            tools = self._card(left)
            tools.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            self._h2(tools, "Tools")
            row = ctk.CTkFrame(tools, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 16))
            self.dump_btn = self._ghost_btn(row, "Dump memory", self._on_dump, 120)
            self.dump_btn.pack(side="left")
            self.recover_btn = self._ghost_btn(row, "Recover LED", self._on_recover, 120)
            self.recover_btn.pack(side="left", padx=8)

            beh = self._card(page)
            beh.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
            self._h2(beh, "Behaviour")
            self.verify_var = ctk.BooleanVar(value=True)
            self.write_poll = ctk.BooleanVar(value=True)
            self.dump_var = ctk.BooleanVar(value=False)
            self.refresh_var = ctk.BooleanVar(value=False)
            self.set_active_var = ctk.BooleanVar(value=False)
            for text, var in (("Verify by reading back", self.verify_var),
                              ("Write polling rate", self.write_poll),
                              ("Dump memory around Apply", self.dump_var),
                              ("Legacy profile bounce", self.refresh_var),
                              ("Write active-profile byte", self.set_active_var)):
                self._switch(beh, text, var).pack(anchor="w", padx=20, pady=5)
            self._note(beh, "The last two are experimental and off by default. The memory dump is also off: it "
                            "is slow and logs a byte diff.", pady=(6, 14), wrap=300)

            logc = self._card(page)
            logc.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
            logc.grid_columnconfigure(0, weight=1)
            logc.grid_rowconfigure(1, weight=1)
            head = ctk.CTkFrame(logc, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 4))
            ctk.CTkLabel(head, text="Debug log", font=F["h2"], text_color=C.TEXT).pack(side="left")
            ctk.CTkButton(head, text="Clear", width=56, height=26, corner_radius=7, font=F["small"],
                          fg_color="transparent", hover_color=C.SURFACE2, text_color=C.MUTED,
                          command=self._clear_log).pack(side="right")
            ctk.CTkButton(head, text="Copy", width=56, height=26, corner_radius=7, font=F["small"],
                          fg_color="transparent", hover_color=C.SURFACE2, text_color=C.MUTED,
                          command=self._copy_log).pack(side="right", padx=4)
            self.dbg_text = ctk.CTkTextbox(logc, height=120, font=F["mono"], fg_color=C.BG, text_color=C.MUTED,
                                           border_width=1, border_color=C.BORDER, corner_radius=8)
            self.dbg_text.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
            self.dbg_text.configure(state="disabled")

        def _copy_log(self):
            self.clipboard_clear()
            self.clipboard_append(self.dbg_text.get("1.0", "end"))
            self._toast("Log copied to the clipboard.", "info")

        def _clear_log(self):
            self.dbg_text.configure(state="normal")
            self.dbg_text.delete("1.0", "end")
            self.dbg_text.configure(state="disabled")

        def _pump(self):
            lines = []
            try:
                while True:
                    lines.append(log_q.get_nowait())
            except queue.Empty:
                pass
            if lines:
                self.dbg_text.configure(state="normal")
                self.dbg_text.insert("end", "\n".join(lines) + "\n")
                total = int(float(self.dbg_text.index("end-1c").split(".")[0]))
                if total > 4000:
                    self.dbg_text.delete("1.0", "%d.0" % (total - 4000))
                self.dbg_text.see("end")
                self.dbg_text.configure(state="disabled")
            try:
                while True:
                    ui_q.get_nowait()()
            except queue.Empty:
                pass
            self.after(50, self._pump)

        def _refresh_endpoints(self, show_details=False):
            try:
                infos = [] if link.dry_run else link.list_interfaces()
            except Exception as exc:
                log("enumerate failed: %r" % (exc,))
                infos = []
            self.endpoint_infos = infos
            labels = [AUTO_LABEL] + [endpoint_label(n, i) for n, i in enumerate(infos)]
            self.endpoint_menu.configure(values=labels)
            if self.endpoint_var.get() not in labels:
                self.endpoint_var.set(AUTO_LABEL)
                link.path_override = None
            if show_details:
                log("--- %d HID collection(s) for %04X:%04X ---" % (len(infos), VID, PID))
                for n, info in enumerate(infos):
                    log("[%d] interface_number=%s usage_page=0x%04X usage=0x%04X" % (
                        n, info.get("interface_number"), info.get("usage_page") or 0, info.get("usage") or 0))
                    log("     path=%s" % _path_text(info))

        def _on_endpoint_change(self, value):
            if value == AUTO_LABEL:
                link.path_override = None
            else:
                labels = [AUTO_LABEL] + [endpoint_label(n, i) for n, i in enumerate(self.endpoint_infos)]
                try:
                    link.path_override = self.endpoint_infos[labels.index(value) - 1]["path"]
                except (ValueError, IndexError):
                    link.path_override = None
            log("endpoint selection: %s" % value)
            self._rescan()

        def _vendor_infos(self):
            return [i for i in link.list_interfaces() if (i.get("usage_page") or 0) >= 0xFF00]

        def _snapshot(self):
            stages = tuple((bool(self.stage_enabled[i].get()), self._stage_value(i)) for i in range(NUM_STAGES))
            uses_colour = MODES.get(self.mode, (0, 0, True))[2]
            return (stages, self.mode, self._color if uses_colour else None,
                    int(self.bright.get()), int(self.speed.get()), int(self.poll_hz))

        def _changed(self, *_a):
            if not self._loading:
                self._refresh_state()

        def _style(self, widget, key, **kw):
            """configure() only when the look really changes (no flicker, no pointless redraws)."""
            if self._style_cache.get(id(widget)) != key:
                self._style_cache[id(widget)] = key
                widget.configure(**kw)

        def _refresh_state(self):
            dirty = self._snapshot() != self._baseline
            if dirty:
                text, fg, bg = "Unsaved changes", C.WARN, C.WARN_DIM
            elif self.pending_reload:
                text, fg, bg = "Saved \u00B7 not loaded yet", C.INFO, C.INFO_DIM
            else:
                text = None
            if text:
                self.state_pill.configure(fg_color=bg)
                self.state_dot.configure(text_color=fg)
                self.state_text.configure(text=text, text_color=fg)
                self.state_pill.grid()
            else:
                self.state_pill.grid_remove()
            if self.busy:
                return
            if dirty:
                self._style(self.apply_btn, "primary", fg_color=C.ACCENT, hover_color=C.ACCENT_HOVER,
                            text_color=C.ON_ACCENT)
            else:
                self._style(self.apply_btn, "quiet", fg_color=C.SURFACE2, hover_color=C.HOVER, text_color=C.TEXT)
            if self.pending_reload and not dirty:
                self._style(self.restart_btn, "hot", fg_color=C.ACCENT_DIM, hover_color=C.ACCENT_DIM,
                            border_color=C.ACCENT, text_color=C.ACCENT)
            else:
                self._style(self.restart_btn, "quiet", fg_color=C.SURFACE2, hover_color=C.HOVER,
                            border_color=C.BORDER, text_color=C.TEXT)

        def _toast(self, text, kind="info", ttl=7000):
            self._toast_id += 1
            tid = self._toast_id
            col = {"info": C.MUTED, "ok": C.ACCENT, "warn": C.WARN, "err": C.DANGER}.get(kind, C.MUTED)
            self.toast_dot.configure(text="\u25CF" if text else "", text_color=col)
            self.toast_msg.configure(text=text)
            if text and kind in ("ok", "info"):
                self.after(ttl, lambda: self._toast_expire(tid))

        def _toast_expire(self, tid):
            if tid == self._toast_id and not self.busy:
                self._toast("")

        def _set_conn(self, kind, text):
            col = {"ok": C.ACCENT, "err": C.DANGER, "warn": C.WARN, "info": C.INFO}.get(kind, C.MUTED)
            self.conn_dot.configure(text_color=col)
            self.conn_text.configure(text=text)

        def _set_busy(self, on: bool):
            self.busy = on
            for b in self._action_buttons:
                b.configure(state="disabled" if on else "normal")
            if on:
                self.progress.configure(mode="indeterminate")
                self.progress.start()
                self.apply_btn.configure(text="Working\u2026")
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress.set(0)
                self.apply_btn.configure(text="Apply")
                self._style_cache.clear()
                self._refresh_state()

        def _run(self, label, fn, done=None, on_error=None, quiet=False):
            if self.busy:
                return
            self._set_busy(True)
            self._toast(label, "info", ttl=60000)

            def work():
                try:
                    res, err = fn(), None
                except Exception as exc:
                    res, err = None, exc
                ui_q.put(lambda: self._finish(res, err, done, on_error, quiet))

            threading.Thread(target=work, daemon=True).start()

        def _finish(self, res, err, done, on_error, quiet):
            self._set_busy(False)
            self._toast("")
            if err is not None:
                log("ERROR: %r" % (err,))
                self._toast("Error: %s" % (str(err).splitlines() or [repr(err)])[0][:120], "err")
                if on_error:
                    on_error(err)
                elif not quiet:
                    messagebox.showerror(APP_NAME, str(err), parent=self)
                return
            if done:
                done(res)

        def _poll_presence(self):
            try:
                if not self.busy:
                    present = link.is_present()
                    if present != self._last_present:
                        self._last_present = present
                        if present:
                            self._refresh_endpoints()
                            self._connect()
                        else:
                            self._mark_disconnected("Mouse not found")
            finally:
                self.after(2500, self._poll_presence)

        def _rescan(self):
            self._last_present = None
            self.after(10, self._poll_presence_once)

        def _poll_presence_once(self):
            if not self.busy:
                self._last_present = link.is_present()
                if self._last_present:
                    self._refresh_endpoints()
                    self._connect()
                else:
                    self._mark_disconnected("Mouse not found")

        def _mark_disconnected(self, why):
            link.close()
            self.connected = False
            self.write_ok = False
            self._set_conn("err", "Not connected")
            self.dev_label.configure(text="04D9:FC30  -  %s" % why)

        def _connect(self):
            def work():
                link.close()
                link.open()
                return handshake(link)

            def done(p):
                self.connected = True
                self.write_ok = True
                if link.dry_run:
                    self._set_conn("info", "Dry run")
                    self.dev_label.configure(text="04D9:FC30  -  DRY RUN, frames are only logged")
                    return
                info = link.info or {}
                self._set_conn("ok", "Connected")
                self.dev_label.configure(text="04D9:FC30  -  interface %s, usage page 0x%04X" % (
                    info.get("interface_number"), info.get("usage_page") or 0))
                self.active_label.configure(text="Mouse active profile byte: %d  (= GUI profile %d if 0-based)" % (p, p + 1))
                self.profile_sel.set(p + 1)
                self._load_profile(p)

            def fail(err):
                if isinstance(err, ProtocolError):
                    self.connected = True
                    self.write_ok = False
                    self._set_conn("warn", "Handshake failed")
                    self.dev_label.configure(text="The mouse did not answer like an M711. See the debug log. "
                                                  "(--force-write overrides this at your own risk)")
                    self._toast("Connected, but the handshake failed - writes are locked.", "warn")
                else:
                    self._mark_disconnected(str(err))
                    if "Could not open" in str(err):
                        self._set_conn("warn", "Detected - cannot open")
                        self._toast("Detected, but could not open it. Close the Redragon software and press Rescan.", "warn")

            self._run("Connecting\u2026", work, done, fail)

        def _on_profile_change(self, value):
            if self.connected and self.write_ok and not link.dry_run:
                self._load_profile(int(value) - 1)

        def _on_read(self):
            if self.connected and not link.dry_run:
                self._load_profile(int(self.profile_sel.get()) - 1)
            else:
                self._toast("Not connected to a mouse.", "warn")

        def _load_profile(self, p):
            def work():
                with link.session():
                    raw = read_profile_raw(link, p)
                    poll = read_poll_raw(link)
                return raw, poll

            def done(res):
                raw, poll = res
                mode, rgb, bright, speed = decode_led(raw["led"])
                self._loading = True
                try:
                    if mode is None:
                        log("unknown LED mode bytes %s - leaving the mode selector alone" % hexs(raw["led"][3:6]))
                    else:
                        self._select_mode(mode, user=False)
                    self._set_color(rgb_to_hex(rgb), user=False)
                    self.bright.set(max(BRIGHT_RANGE[0], min(BRIGHT_RANGE[1], bright)))
                    self.speed.set(max(SPEED_RANGE[0], min(SPEED_RANGE[1], speed)))
                    for i, st in enumerate(decode_dpi(raw["dpi"])):
                        self.stage_enabled[i].set(st.enabled)
                        v = snap_dpi(st.dpi)
                        self.stage_dpi[i] = v
                        self.stage_var[i].set(str(v))
                        self.stage_slider[i].set(v)
                    hz = decode_poll(poll, p)
                    if hz:
                        self._set_poll(hz, user=False)
                finally:
                    self._loading = False
                for i in range(NUM_STAGES):
                    self._apply_stage_look(i)
                    self._update_eff(i)
                self._schedule_ladder()
                self._baseline = self._snapshot()
                self._refresh_state()
                self._toast("Loaded profile %d from the mouse." % (p + 1), "info")

            self._run("Reading profile %d\u2026" % (p + 1), work, done)

        def _on_dump(self):
            if self.busy or not self.connected:
                self._toast("Not connected to a mouse.", "warn")
                return

            def work():
                d = dump_memory(link)
                path = None if link.dry_run else save_dump(d, "manual", BACKUP_DIR)
                return d, path

            def done(res):
                d, path = res
                log("--- memory dump 0x%04X-0x%04X ---" % (DUMP_START, DUMP_END - 1))
                for line in dump_to_text(d).splitlines():
                    log(line)
                if path:
                    log("dump saved: %s" % path)
                self._toast("Memory dumped%s." % (" -> %s" % path if path else ""), "ok")

            self._run("Dumping memory\u2026", work, done)

        def _targets(self):
            return list(range(NUM_PROFILES)) if self.all_profiles.get() else [int(self.profile_sel.get()) - 1]

        def _writes_allowed(self):
            if link.dry_run or self.write_ok or args.force_write:
                return True
            if not self.connected:
                self._toast("Mouse not connected.", "warn")
                return False
            messagebox.showwarning(APP_NAME, "Writes are locked because the mouse did not pass the handshake.\n"
                                             "Check the debug log or start with --force-write.", parent=self)
            return False

        def _gather_config(self):
            stages = []
            for i in range(NUM_STAGES):
                try:
                    v = snap_dpi(int(float(self.stage_var[i].get().strip())))
                except ValueError:
                    messagebox.showerror(APP_NAME, "Stage %d: DPI is not a number." % (i + 1), parent=self)
                    return None
                stages.append(DpiStage(bool(self.stage_enabled[i].get()), v))
            if not any(s.enabled for s in stages):
                messagebox.showerror(APP_NAME, "Enable at least one DPI stage.", parent=self)
                return None
            h = normalize_hex(self.hex_var.get())
            if not h and MODES[self.mode][2]:
                messagebox.showerror(APP_NAME, "Colour must be a hex code like #ff8800.", parent=self)
                return None
            h = h or self._color
            for i, st in enumerate(stages):
                self.stage_dpi[i] = st.dpi
                self.stage_var[i].set(str(st.dpi))
                self.stage_slider[i].set(st.dpi)
                self._update_eff(i)
            self._set_color(h, user=False)
            led = LedSettings(self.mode, hex_to_rgb(h), int(self.bright.get()), int(self.speed.get()))
            return Config(stages, led, int(self.poll_hz), bool(self.write_poll.get()))

        def _on_apply(self):
            if self.busy or not self._writes_allowed():
                return
            cfg = self._gather_config()
            if cfg is None:
                return
            snap = self._snapshot()
            profiles = self._targets()
            broadcast = bool(self.broadcast_var.get())
            set_active = (int(self.profile_sel.get()) - 1) if self.set_active_var.get() else None
            dump = bool(self.dump_var.get())
            autorestart = bool(self.autorestart_var.get())
            refresh = bool(self.refresh_var.get()) and not autorestart
            verify = bool(self.verify_var.get())

            def write_flash():
                if broadcast and not link.dry_run:
                    targets = self._vendor_infos()
                    if not targets:
                        raise LinkError("Broadcast: no vendor endpoint (usage page >= 0xFF00) found.")
                    merged = {"backup": None, "mismatches": [], "profiles": profiles, "diff": [],
                              "dump_files": [], "endpoints_ok": 0, "endpoints": len(targets), "changed": False,
                              "dpi_changed": False}
                    for n, info in enumerate(targets):
                        same = link.info is not None and link.info.get("path") == info["path"]
                        l2 = link if same else MouseLink(log, dry_run=False, path_override=info["path"])
                        tag = endpoint_label(n, info)
                        log("=== broadcast to %s ===" % tag)
                        try:
                            if not same:
                                l2.open()
                            res = apply_config(l2, cfg, profiles, verify, BACKUP_DIR, dump=dump,
                                               set_active=set_active, refresh=refresh)
                            merged["backup"] = merged["backup"] or res["backup"]
                            merged["mismatches"] += ["%s: %s" % (tag, m) for m in res["mismatches"]]
                            merged["diff"] += ["%s: %s" % (tag, m) for m in res["diff"]]
                            merged["dump_files"] += res["dump_files"]
                            merged["endpoints_ok"] += 1
                            merged["changed"] = merged["changed"] or res.get("changed", True)
                            merged["dpi_changed"] = merged["dpi_changed"] or res.get("dpi_changed", False)
                        except (LinkError, ProtocolError, ValueError) as exc:
                            log("broadcast: %s failed: %s" % (tag, exc))
                            merged["mismatches"].append("%s: %s" % (tag, exc))
                        finally:
                            if not same:
                                l2.close()
                    return merged
                return apply_config(link, cfg, profiles, verify, BACKUP_DIR, dump=dump, set_active=set_active,
                                    refresh=refresh)

            def work():
                writes_before = link.writes
                try:
                    res = write_flash()
                except Exception as exc:
                    if link.writes > writes_before and not link.dry_run:
                        log("Apply failed after writing (%r) - restarting the mouse to recover it" % (exc,))
                        ui_q.put(lambda: self._toast("Something went wrong - restarting the mouse\u2026", "warn", ttl=60000))
                        try:
                            restart_mouse(link)
                        except (RestartError, LinkError, ProtocolError) as exc2:
                            raise LinkError("%s\n\nThe recovery restart failed too: %s\n"
                                            "Unplug and replug the mouse." % (exc, exc2))
                        raise LinkError("%s\n\nThe mouse was restarted to recover it. Check your settings." % exc)
                    raise
                changed = res.get("changed", True)
                need_restart = bool(res.get("dpi_changed")) or (autorestart and changed)
                res["restart_requested"] = need_restart
                res["restart_error"] = None
                res["restart_profile"] = None
                if need_restart:
                    ui_q.put(lambda: self._toast("Saved. Restarting the mouse (USB)\u2026", "info", ttl=60000))
                    try:
                        res["restart_profile"] = restart_mouse(link)
                    except (RestartError, LinkError, ProtocolError) as exc:
                        log("RESTART FAILED: %s" % exc)
                        res["restart_error"] = str(exc)
                return res

            def done(res):
                rerr = res.get("restart_error")
                problems = []
                if res["mismatches"]:
                    problems.append("Written, but read-back differs:\n" + "\n".join(res["mismatches"][:8]))
                if rerr:
                    problems.append("Saved to the mouse, but the automatic restart failed:\n%s" % rerr)
                self._baseline = snap
                if rerr:
                    self.pending_reload = True
                    self._toast("Saved, but the USB restart failed.", "err")
                    if link.dev is None and not link.dry_run:
                        self._set_conn("warn", "Reconnecting\u2026")
                        self._rescan()
                    self._refresh_state()
                elif res.get("restart_requested"):
                    self._after_reload(res["restart_profile"], "Applied & Reloaded Live!")
                elif link.dry_run:
                    self._toast("Dry run: frames were logged, nothing was sent.", "info")
                    self._refresh_state()
                elif res.get("changed", True):
                    self.pending_reload = True
                    self._toast("Saved. Restart the mouse to load it.", "ok")
                    self._refresh_state()
                else:
                    self._toast("Nothing changed - already up to date.", "info")
                    self._refresh_state()
                if problems:
                    messagebox.showwarning(APP_NAME, "\n\n".join(problems), parent=self)

            self._run("Writing to the mouse\u2026", work, done)

        def _after_reload(self, profile, text):
            """GUI state after a successful USB restart + reconnect."""
            self.connected = True
            self.write_ok = True
            self._last_present = True
            self.pending_reload = False
            self._refresh_endpoints()
            if link.dry_run:
                self._set_conn("info", "Dry run")
            else:
                self._set_conn("ok", "Connected")
                info = link.info or {}
                self.dev_label.configure(text="04D9:FC30  -  interface %s, usage page 0x%04X (reconnected)" % (
                    info.get("interface_number"), info.get("usage_page") or 0))
                self.active_label.configure(text="Mouse active profile byte: %d  (= GUI profile %d if 0-based)"
                                                 % (profile, profile + 1))
            self._toast("Dry run: frames were logged, nothing was sent." if link.dry_run else text,
                        "info" if link.dry_run else "ok")
            self._refresh_state()

        def _on_restart_now(self):
            if self.busy:
                return

            def work():
                return restart_mouse(link)

            def done(profile):
                self._after_reload(profile, "Mouse restarted & reloaded")

            def fail(err):
                self._toast("Restart failed.", "err")
                messagebox.showerror(APP_NAME, "Could not restart the mouse:\n\n%s" % err, parent=self)
                if link.dev is None and not link.dry_run:
                    self._set_conn("warn", "Reconnecting\u2026")
                    self._rescan()

            self._run("Restarting the mouse (USB)\u2026", work, done, fail)

        def _on_recover(self):
            if self.busy or not self._writes_allowed():
                return
            profiles = self._targets()

            def done(_r):
                self.pending_reload = True
                self._on_read()

            self._run("Recovering LED\u2026", lambda: recover_led(link, profiles), done)

        def _on_restore(self):
            if self.busy or not self._writes_allowed():
                return
            path = filedialog.askopenfilename(parent=self, title="Choose a backup", initialdir=BACKUP_DIR,
                                              filetypes=[("Backup", "*.json")])
            if not path:
                return
            if not messagebox.askokcancel(APP_NAME, "Write this backup back to the mouse?\n%s" % path, parent=self):
                return

            def done(n):
                self.pending_reload = True
                self._toast("Restored %d profile(s). Restart the mouse to load them." % n, "ok")
                self._on_read()

            self._run("Restoring backup\u2026", lambda: restore_backup(link, path), done)

        def _on_close(self):
            link.close()
            self.destroy()

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    log("%s %s started%s" % (APP_NAME, __version__, " (DRY RUN)" if args.dry_run else ""))
    from volt_backend import hid as _hid, HID_IMPORT_ERROR
    if _hid is None and not args.dry_run:
        log("Python module 'hid' missing: %s  ->  py -m pip install hidapi" % HID_IMPORT_ERROR)
    App().mainloop()
    return 0
