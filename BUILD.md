# VOLT - build notes (Windows 10/11)

## Layout
```
volt.py            entry point + CLI flags (--debug --probe --dump --find-active --dry-run ...)
volt_ui.py         the GUI (customtkinter)
volt_backend.py    protocol, HID link, apply / restart / recovery (logic unchanged)
assets/            volt.ico (exe + window icon), logo.png (sidebar), icon.png (original)
VOLT.spec          PyInstaller spec     version_info.txt   exe properties
build.bat          one-click build      requirements.txt   dependencies
tests/             py -m unittest discover -s tests   (no mouse, no display needed)
```

## Run from source
```
py -m pip install customtkinter hidapi Pillow
py -m pip uninstall hid          (only if installed: it clashes with hidapi)
py volt.py                       (run in an Administrator terminal, or click "Relaunch as admin" in the banner)
py volt.py --dry-run             (nothing is sent to the mouse)
```

## Build the exe
Double-click `build.bat`, or:
```
py -m venv .venv && .venv\Scripts\activate
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm --clean VOLT.spec
```
Result: `dist\VOLT\VOLT.exe` (zip the whole `dist\VOLT` folder to share it).

Equivalent single command without the spec:
```
py -m PyInstaller --noconfirm --clean --name VOLT --windowed --uac-admin --noupx ^
  --icon assets\volt.ico --version-file version_info.txt --add-data "assets;assets" ^
  --collect-data customtkinter --hidden-import hid --hidden-import volt_ui --hidden-import volt_backend volt.py
```

### onedir (default, recommended) vs onefile
* onedir: starts instantly, far fewer antivirus detections, easy to patch. Ship as a zip or an Inno Setup installer.
* onefile: set `ONEFILE = True` in VOLT.spec -> `dist\VOLT.exe`. It unpacks to %TEMP% on every launch (2-5 s start-up) and is flagged more often.

### Behaviour of the exe
* `--uac-admin`: Windows shows the UAC prompt at every launch (the shield on the icon is normal). Needed for `pnputil /restart-device`.
* No console: stray `print()` is swallowed; CLI flags write to `%APPDATA%\RedragonM711\volt-cli.log`. Use `py volt.py --probe` from source for live diagnostics.
* Crashes are written to `%APPDATA%\RedragonM711\volt-crash.log` and shown in a dialog.
* Settings, backups: `%APPDATA%\RedragonM711\` (same folder as before, so existing backups still work).

## Antivirus false positives
A tool that talks to USB, launches PowerShell/pnputil and demands admin looks suspicious to heuristics. To reduce it:
1. Build in a fresh venv, use onedir, keep UPX off (the spec does).
2. Build PyInstaller's bootloader yourself (`pip install pyinstaller --no-binary pyinstaller`, needs a C compiler); the prebuilt one is widely signatured.
3. Code-sign the exe with a real certificate (self-signed does not help).
4. Submit the file at https://www.microsoft.com/wdsi/filesubmission (and to the vendor that flags it) and publish the SHA-256.
5. Distribute the source alongside it.
