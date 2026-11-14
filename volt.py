#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
VOLT - configurator for the Redragon M711 / RD-S131 (Holtek 04D9:FC30), Windows 10/11.

    py volt.py                      GUI
    py volt.py --dry-run            full GUI, but NOTHING is sent to the mouse (frames are only logged)
    py volt.py --debug              list every HID interface of 04D9:FC30 (no traffic is sent)
    py volt.py --probe              read-only handshake + dump of the active profile
    py volt.py --dump               read-only dump of the mouse's config memory to a text file
    py volt.py --find-active        find the active-DPI-stage byte (no writes; interactive, needs a console)
    py volt.py --active-dpi-addr 0x00NN   GUI, with live DPI-stage refresh enabled
    py volt.py --interface N        force a USB interface number     --force-write  allow writes after a failed handshake

Protocol: reverse-engineered by others (dokutan/mouse_m908, ernesto-elsaesser/usb-mouse-config,
SK2674/m711-port). Treat this program as GPL-3.0 if you redistribute it.

In the PyInstaller build (no console) the text output of --debug / --probe / --dump goes to
%APPDATA%\RedragonM711\volt-cli.log instead of a console window.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys

import volt_backend as vb


def _fix_stdio(cli: bool) -> None:
    """A --noconsole PyInstaller exe starts with sys.stdout / sys.stderr = None.
    CLI mode: write to a log file.  GUI mode: swallow stray prints."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    fh = None
    if cli:
        try:
            os.makedirs(vb.BACKUP_DIR, exist_ok=True)
            fh = open(os.path.join(vb.BACKUP_DIR, "volt-cli.log"), "w", encoding="utf-8", buffering=1)
        except OSError:
            fh = None
    if fh is None:
        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def _set_app_id() -> None:
    """Own taskbar identity, so Windows shows VOLT's icon instead of grouping it under python.exe."""
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(vb.APP_ID)
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="VOLT - Redragon M711 (04D9:FC30) configurator")
    ap.add_argument("--debug", action="store_true", help="list HID interfaces of 04D9:FC30 and exit (no traffic)")
    ap.add_argument("--probe", action="store_true", help="read-only handshake and profile dump, then exit")
    ap.add_argument("--dump", action="store_true", help="read-only dump of config memory to a text file, then exit")
    ap.add_argument("--find-active", action="store_true",
                    help="interactive: locate the active-DPI-stage byte by pressing the mouse buttons (no writes)")
    ap.add_argument("--active-dpi-addr", type=lambda v: int(v, 0), default=None,
                    help="memory address of the active DPI stage byte (found with --find-active)")
    ap.add_argument("--dry-run", action="store_true", help="run the GUI but send nothing to the mouse")
    ap.add_argument("--interface", type=int, default=None, help="force a USB interface number (default: auto, MI_02)")
    ap.add_argument("--force-write", action="store_true", help="allow writes even if the handshake failed (risky)")
    return ap


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cli = any(a in raw for a in ("--debug", "--probe", "--dump", "--find-active", "-h", "--help"))
    _fix_stdio(cli)
    args = build_parser().parse_args(raw)
    if args.active_dpi_addr is not None:
        vb.ADDR_ACTIVE_DPI = args.active_dpi_addr
    if args.find_active:
        return vb.cmd_find_active(args)
    if args.debug:
        return vb.cmd_debug(args)
    if args.probe:
        return vb.cmd_probe(args)
    if args.dump:
        return vb.cmd_dump(args)

    _set_app_id()
    try:
        from volt_ui import run_gui, write_crash_log
    except Exception:
        import traceback
        _crash(traceback.format_exc())
        return 1
    try:
        return run_gui(args)
    except Exception:
        import traceback
        text = traceback.format_exc()
        write_crash_log(text)
        _crash(text)
        return 1


def _crash(text: str) -> None:
    """Last resort: with no console the user would otherwise see nothing at all."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(vb.APP_NAME, "VOLT crashed:\n\n%s\n\n(also saved to %s)" % (
            text[-1500:], os.path.join(vb.BACKUP_DIR, "volt-crash.log")))
        root.destroy()
    except Exception:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
