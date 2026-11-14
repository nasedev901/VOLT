# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

ONEFILE = False

HERE = os.path.abspath(os.getcwd())

datas = collect_data_files("customtkinter")            # themes, fonts, icons that customtkinter loads at runtime
datas += [
    ("assets/volt.ico", "assets"),
    ("assets/icon.png", "assets"),
    ("assets/logo.png", "assets"),
]

a = Analysis(
    ["volt.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=["hid", "volt_backend", "volt_ui", "darkdetect"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "matplotlib", "scipy", "pandas", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    name="VOLT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX-packed exes are flagged far more often by antivirus
    console=False,                   # --windowed / --noconsole
    uac_admin=True,                  # manifest: requireAdministrator -> UAC prompt on launch (pnputil needs it)
    icon="assets/volt.ico",
    version="version_info.txt",
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VOLT")

