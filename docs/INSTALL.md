# Installing the MED17.7.5 Flash Tool on your desktop

The app ships as a **single standalone executable** – it already contains the
Python backend, the web UI and the ECU profiles, so you do **not** need to
install Python, Node or anything else. Download one file, run it, done.

When it starts it prints a local URL and opens the **MED17 Flash Tool** UI in a
window (or your browser). A small console window stays open showing the log and
the local address – closing it stops the app.

> ⚠️ Read [`SAFETY.md`](SAFETY.md) before writing to a real ECU.

---

## Where to download

* **Releases page** (recommended): the repository's **Releases** section has a
  prebuilt file per platform, published automatically from a `v*` tag:
  * `med17flasher-desktop-windows.exe` – Windows
  * `med17flasher-setup-windows.exe`   – Windows installer (Start‑menu + desktop shortcut)
  * `med17flasher-desktop-macos`       – macOS
  * `med17flasher-desktop-linux`       – Linux
* **Actions artifacts**: every push to the dev branch also builds the same
  binaries. Open the latest **“Build desktop app”** run under the repo’s
  **Actions** tab and download the artifact for your OS (artifacts are `.zip`‑wrapped
  by GitHub – unzip to get the executable).

---

## Windows

**Option A – installer (classic setup):**
1. Download `med17flasher-setup-windows.exe`.
2. Double‑click it and follow the wizard. It installs to your user profile (no
   admin needed), adds a **Start‑menu** entry and, if you tick the box, a
   **desktop shortcut**.
3. Launch **“MED17 Flash Tool”** from the Start menu.

**Option B – portable (no install):**
1. Download `med17flasher-desktop-windows.exe`.
2. Double‑click it. Windows SmartScreen may show *“Windows protected your PC”*
   because the build isn’t code‑signed – click **More info → Run anyway**.
3. The tool opens; keep the little console window open while you use it.

## macOS

1. Download `med17flasher-desktop-macos`.
2. Make it executable and remove the download quarantine, then run it:
   ```bash
   chmod +x ~/Downloads/med17flasher-desktop-macos
   xattr -d com.apple.quarantine ~/Downloads/med17flasher-desktop-macos 2>/dev/null || true
   ~/Downloads/med17flasher-desktop-macos
   ```
   Or in Finder: **right‑click → Open → Open** to get past Gatekeeper the first
   time (the build isn’t notarised).

## Linux

**Quick (portable):**
```bash
chmod +x ./med17flasher-desktop-linux
./med17flasher-desktop-linux
```

**Install into your app menu (per‑user, no root):**
```bash
# from the packaging/ folder next to the downloaded binary:
sh packaging/install_linux.sh ./med17flasher-desktop-linux
```
This copies the binary to `~/.local/bin`, installs the icon and a `.desktop`
entry so **“MED17 Flash Tool”** appears in your application menu.

To talk to real hardware you’ll typically want SocketCAN
(`sudo apt install can-utils`) or a `python-can` adapter; the app runs fine
against the built‑in simulator with none of that.

---

## Run from source (any OS)

If you’d rather run from the checkout instead of a prebuilt binary:

```bash
pip install -e .            # core only, no third-party deps
med17flasher desktop        # opens the UI in a window/browser
```

Optional: `pip install pywebview` for a native window instead of the browser,
and `pip install -e ".[all]"` for the `python-can`/`pyserial`/YAML extras.

## Build the binary yourself

```bash
make desktop                # -> dist/med17flasher-desktop
```
On Windows, to also produce the setup installer:
```powershell
choco install innosetup -y
iscc packaging\windows_installer.iss   # -> dist\installer\med17flasher-setup.exe
```
