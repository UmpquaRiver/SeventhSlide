# Building SeventhSlide

SeventhSlide is a FastAPI/uvicorn backend (`lyrics.py`) hosted in an Electron
desktop shell (`electron/`). The shell starts the backend, opens the admin window,
and pushes outputs fullscreen onto physical monitors.

Packaging is two steps: freeze the backend into a standalone binary with
PyInstaller, then bundle it together with the Electron shell using electron-builder.

**Frozen binaries cannot be cross-compiled — build each OS's artifact on that OS**
(or a CI runner for it).

## Prerequisites

```bash
pip install -r requirements.txt   # backend (FastAPI/uvicorn)
pip install pyinstaller           # freezes the backend
npm install                       # Electron + electron-builder
```

App icons live in `icons/` and are committed, so builds work as-is. After changing
the logo (`icons/seventhslide.png`), regenerate them with `python icons/make_icons.py`.

## Running from source (dev)

```bash
npm start
```

This launches the Electron shell, which spawns `python lyrics.py` as the backend
(override the interpreter with `SEVENTHSLIDE_PYTHON`). The admin window opens on
`http://127.0.0.1:49777/admin`; that URL is also reachable from other devices on
the network and from OBS.

## Building a distributable

1. **Freeze the backend** into a single executable:
   ```bash
   pyinstaller lyrics.spec        # → dist/lyrics-slideshow  (.exe on Windows)
   ```
2. **Bundle the app** with electron-builder:
   ```bash
   npm run build                  # → electron-dist/
   ```
   `package.json` wires the frozen backend in as an extra resource, so the shell
   runs it as `lyrics-slideshow` next to the app instead of needing system Python.
   Targets per OS: **unpacked folder** (Windows — wrapped into a setup wizard by
   Inno Setup, below), **AppImage** (Linux), **dmg** (macOS).

   electron-builder is only the packager — it isn't tied to any one installer. On
   Windows we emit a plain app folder (`win.target: "dir"`) and wrap it with our
   own Inno Setup wizard; switch `win.target` to `"nsis"` if you'd rather use
   electron-builder's built-in installer instead.

Bump `version` in `package.json` for each release.

The backend writes user data (database, exports, uploads) to the per-user OS dir
(`%APPDATA%\SeventhSlide`, `~/Library/Application Support/SeventhSlide`,
`~/.local/share/SeventhSlide`), so the installed app can live read-only.

## Per platform

### Windows

Build the app folder, then wrap it in the setup wizard:

```powershell
pyinstaller lyrics.spec                    # → dist\lyrics-slideshow.exe
npm run build                              # → electron-dist\win-unpacked\
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\SeventhSlide.iss
```

`installer\SeventhSlide.iss` ([Inno Setup](https://jrsoftware.org/isinfo.php))
packages `electron-dist\win-unpacked\` into
`installer-output\SeventhSlide-<version>-Setup.exe`. It installs into
`Program Files\SeventhSlide`, adds Start Menu shortcuts and a clean uninstall
entry, and offers an **optional checkbox to place the user manual (PDF) on the
Desktop**. Install the compiler once with
`winget install --id JRSoftware.InnoSetup -e`, and bump `MyAppVersion` at the top
of the `.iss` per release (keep it in step with `package.json`); the fixed `AppId`
GUID lets new versions upgrade in place.

The setup `.exe` is unsigned; sign it (`signtool`, or Inno Setup's `SignTool`
directive) before wide distribution to avoid the SmartScreen prompt.

**Test without touching your machine** (silent install into a temp dir, then
uninstall):
```powershell
$dir = "$env:TEMP\ss_test"
Start-Process installer-output\SeventhSlide-1.1.0-Setup.exe `
  -ArgumentList '/VERYSILENT','/CURRENTUSER',"/DIR=$dir",'/TASKS=' -Wait   # add '/TASKS=desktopmanual' to test the Desktop-PDF copy
Start-Process "$dir\unins000.exe" -ArgumentList '/VERYSILENT' -Wait
```

### macOS

```bash
pyinstaller lyrics.spec
npm run build                     # → electron-dist/SeventhSlide-<version>.dmg
```
For distribution, sign and notarize the app; otherwise users must allow it in
**System Settings → Privacy & Security** on first launch.

### Linux

```bash
pyinstaller lyrics.spec
npm run build                     # → electron-dist/SeventhSlide-<version>.AppImage
```
Install fontconfig for correct font rendering: `sudo apt-get install fontconfig`.

By default the shell uses Chromium's X11 backend (XWayland on a Wayland session),
the most reliable path for placing a fullscreen output window on a *specific*
physical monitor. Opt into native Wayland with `SEVENTHSLIDE_OZONE=wayland`, or
force X11 with `SEVENTHSLIDE_OZONE=x11`.

## Headless backend (optional)

For server/OBS/browser use with no desktop window, run the frozen backend on its
own — it serves the same admin and output pages:

```bash
pyinstaller lyrics.spec
./dist/lyrics-slideshow            # listens on http://0.0.0.0:49777/
```

## Troubleshooting

- **`ModuleNotFoundError` in the frozen backend** — add the module to
  `lyrics.spec`'s `hiddenimports`.
- **GPU / scroll stutter** — press `Ctrl+Shift+G` in the app to open
  `chrome://gpu` and confirm "Rasterization: Hardware accelerated".
- **Fonts not rendering** — enable "Bundle Local Fonts" in app settings; on Linux
  ensure fontconfig is installed.
- **Database locked** — only one instance may run at a time (the shell enforces a
  single-instance lock).
</content>
