# Manual setup

What `economist_setup.py` does for you, step by step, for when you would rather
do it by hand or the tool cannot run. For the usual setup, see the
[README](../README.md#setup).

One time: harvest a browser export, import it, seed the profile.

## The steps

> **`__cf_bm` is HttpOnly**, so `document.cookie` in the Console cannot see it.
> It appears only in the Network panel's request headers.

1. Open your Chromium at <https://www.economist.com/weeklyedition> and log in.
2. DevTools (<kbd>F12</kbd>) → **Network**, clear the text filter, click **`Doc`**.
3. **Reload twice** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>). The first
   reload *receives* a new `__cf_bm`; the second one *sends* it. A copy taken after
   only one reload has no `__cf_bm` and is rejected.
4. Right-click the top document row (after the redirect it is named for the edition
   date, e.g. `2026-09-05`) → **Copy** → **Copy as cURL**.
5. Check the copy is usable (`1` = good):

   ```bash
   wl-paste | grep -c __cf_bm      # xclip -o on X11
   ```

   On macOS:

   ```bash
   pbpaste | grep -c __cf_bm
   ```

   On Windows, Chrome offers two flavours: pick **Copy as cURL (bash)**, not
   (cmd). Then in PowerShell (`True` = good):

   ```powershell
   (Get-Clipboard -Raw) -match '__cf_bm'
   ```

6. Import and seed, on the **host** (not inside a flatpak). Skip the
   `git clone`, `cd` and `uv venv` lines if you already did the README's
   [step 1](../README.md#1-get-the-code).

   ```bash
   git clone https://github.com/CR0CKER/calibre-economist-cdp ~/calibre-economist-cdp
   cd ~/calibre-economist-cdp
   wl-paste > /run/user/$(id -u)/eco-curl.txt
   python3 import_curl_cookies.py /run/user/$(id -u)/eco-curl.txt
   python3 economist_session.py --seed
   ```

   On macOS, use the per-user `$TMPDIR` for the temporary export:

   ```bash
   pbpaste > "$TMPDIR/eco-curl.txt"
   python3 import_curl_cookies.py "$TMPDIR/eco-curl.txt"
   rm "$TMPDIR/eco-curl.txt"
   python3 economist_session.py --seed
   ```

   On Windows, in PowerShell, with a [uv](https://docs.astral.sh/uv/) virtual
   environment in the checkout (the recipe finds `.venv` there by itself):

   ```powershell
   git clone https://github.com/CR0CKER/calibre-economist-cdp $HOME\calibre-economist-cdp
   cd $HOME\calibre-economist-cdp
   uv venv
   Get-Clipboard -Raw | Set-Content -NoNewline "$env:TEMP\eco-curl.txt"
   .venv\Scripts\python.exe import_curl_cookies.py "$env:TEMP\eco-curl.txt"
   .venv\Scripts\python.exe economist_session.py --seed
   ```

   `--seed` launches the browser, navigates, waits for the site to accept the
   session, and writes the renewed cookie file. It also records its own location
   in calibre's config dir so the recipe can find it. If the helper reports
   `BLOCKED`, see [Troubleshooting](../README.md#troubleshooting).

## Flatpak calibre

The recipe has to start a program on the host from inside calibre's sandbox.
Grant that once:

```bash
flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre
```

calibre already holds `filesystems=host` and `devices=all`, so this widens little
in practice, but it is a real sandbox escape and you should know it is there.

## Installing the recipe into calibre

Fetch news → **Add a custom news source** → **Switch to advanced mode** → paste the
contents of `economist.recipe` → give it a title such as "The Economist (CDP)" →
Add/Update recipe. Then schedule *that* recipe, not the built-in one.

To update an installed copy later, edit the same custom recipe in place (or from the
command line, `update_custom_recipe(<id>, title, source)` via `calibre-debug`).
Do not add a second copy; a scheduled recipe is tied to its id.
