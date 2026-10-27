# The Economist for calibre, through your own Chromium

Last updated: 2026-10-25 04:15 AM CDT

[![ci](https://github.com/CR0CKER/calibre-economist-cdp/actions/workflows/ci.yml/badge.svg)](https://github.com/CR0CKER/calibre-economist-cdp/actions/workflows/ci.yml)
[![license](https://img.shields.io/github/license/CR0CKER/calibre-economist-cdp)](LICENSE)
[![release](https://img.shields.io/github/v/release/CR0CKER/calibre-economist-cdp)](https://github.com/CR0CKER/calibre-economist-cdp/releases)

A calibre recipe that downloads the current weekly edition of **The Economist**,
with images, on demand or on a schedule. It replaces calibre's built-in
Economist recipe, which has been blocked since late August 2026: every request
goes through a real Chrome or Chromium that you already have installed, driven
over the Chrome DevTools Protocol.

**Status:** working. Last verified 2026-09-09 against the edition of 2026-09-05:
full articles with images, 0 failures, 0 HTTP 403s. Discussion: MobileRead thread
[Economist failed after upgrade to 9.10](https://www.mobileread.com/forums/showthread.php?t=374136).

You need an Economist **subscription**. This project shares code only; no
downloaded editions are or will be distributed.

## How it works

1. You copy your logged-in session out of your browser once. The importer saves
   it to calibre's config folder.
2. `--seed` loads your login cookies into a **dedicated** Chromium profile (never
   your everyday one) and checks that the site accepts it.
3. On each download, the recipe starts that browser off-screen, refreshes the
   session, and fetches every article and image from inside the browser. Then
   it shuts the browser down.

Your login cookies last for months, so scheduled downloads run unattended.

Why the built-in recipe fails, how this works in detail, and the traps found on
the way: [docs/design.md](docs/design.md).

## Requirements

- An Economist subscription
- calibre 8 or 9 (tested on 9.7 and 9.14)
- Google Chrome or Chromium
- Python 3.10+. The runtime needs no third-party packages.
- On Windows, [uv](https://docs.astral.sh/uv/) for the virtual environment
- Linux, macOS or Windows. Linux is where this was built and is tested. macOS
  ([#9](https://github.com/CR0CKER/calibre-economist-cdp/pull/9)) and Windows
  ([#15](https://github.com/CR0CKER/calibre-economist-cdp/pull/15)) were
  contributed and verified end to end by their authors; what differs is in
  docs/design.md under [macOS](docs/design.md#macos) and
  [Windows](docs/design.md#windows).

## Setup

### 1. Get the code

```bash
git clone https://github.com/CR0CKER/calibre-economist-cdp ~/calibre-economist-cdp
cd ~/calibre-economist-cdp
```

On Windows, in PowerShell, also create the [uv](https://docs.astral.sh/uv/)
virtual environment that the recipe will use:

```powershell
git clone https://github.com/CR0CKER/calibre-economist-cdp $HOME\calibre-economist-cdp
cd $HOME\calibre-economist-cdp
uv venv
```

Keep the checkout where it is: calibre finds the helper by this path. If you
move it, run `--seed` again.

### 2. Run the setup tool

```bash
python3 economist_setup.py                   # Linux, macOS
.venv\Scripts\python.exe economist_setup.py  # Windows
```

Run it on the host, not inside a flatpak or toolbox shell. It walks you through
four steps:

1. **Checks** Python, the browser and calibre's config folder. On a flatpak
   calibre, it offers to grant the permission calibre needs to start the
   browser.
2. **Imports your session.** It tells you what to do in Chrome: log in at
   <https://www.economist.com/weeklyedition>, open DevTools → **Network** →
   **Doc**, reload **twice**, then right-click the top row → **Copy as cURL**
   (on Windows, **Copy as cURL (bash)**). It reads the copy straight from the
   clipboard. If the copy is wrong, it tells you why and lets you copy again.
3. **Seeds** the dedicated browser profile. A browser window opens briefly.
4. **Puts the recipe on the clipboard** and tells you where to paste it in
   calibre: **Fetch news** → **Add a custom news source** → **Switch to
   advanced mode**. Replace the template with the clipboard contents, title it
   "The Economist (CDP)", and click **Add/Update recipe**.

Then download or schedule **that** recipe, not the built-in one.

The clipboard holds your login until the tool replaces it with the recipe. A
clipboard history keeps it after that: on Windows clear it in Win+V (and turn
off clipboard sync across devices, which uploads it), on Linux in your
clipboard manager, if you run one.

On Linux the clipboard needs `wl-clipboard` (Wayland) or `xclip`. Without
either, save the cURL copy to a file and run
`economist_setup.py --from-file <file>`. The tool offers to delete that file
afterwards, since it holds your login.

To update the recipe later, edit the same custom recipe in place rather than
adding a second copy: a schedule is tied to the recipe it was set on. Changes
to the `.py` files need no reinstall. For the same reason, when you run the
setup tool again later, for example after a block, skip its step 4 if the
recipe is already installed.

To do all of this by hand instead, see [docs/manual-setup.md](docs/manual-setup.md).

## Everyday use

Download or schedule the recipe in calibre as usual. To check or refresh the
session by hand:

```bash
python3 economist_session.py --status    # what the session last did
python3 economist_session.py --refresh   # renew now; exits 0 when it works
python3 economist_session.py --stop      # close a browser left running
```

On Windows, use `.venv\Scripts\python.exe` in place of `python3`.

Download no more than one edition at a time, with hours between attempts.
Rapid repeated sessions can get your IP a temporary captcha.

## Configuration

Set these in the environment that runs calibre. All are optional.

| Variable | Meaning | Default |
|---|---|---|
| `ECONOMIST_BROWSER_CMD` | Command that starts the browser, e.g. `google-chrome`; quote paths with spaces | Linux: ungoogled-chromium flatpak; macOS/Windows: installed Chrome or Chromium |
| `ECONOMIST_CHROME_PROFILE` | The dedicated profile directory (never your everyday profile) | Linux/macOS: `~/.var/app/io.github.ungoogled_software.ungoogled_chromium/config/chromium-economist`; Windows: `%LOCALAPPDATA%\calibre-economist-cdp\chrome-profile` |
| `ECONOMIST_PYTHON` | Python used to run the helper | `python3`; Windows: `.venv\Scripts\python.exe` in the checkout, else `py` or `python` on `PATH` |
| `ECONOMIST_SESSION_SCRIPT` | Path to `economist_session.py` | recorded by `--seed` |
| `ECONOMIST_SERVE_MAX_S` | Hard limit in seconds before a served browser is closed | 2700 |
| `ECONOMIST_DEBUG` | `1` prints retry and teardown details to stderr | off |
| `CALIBRE_CONFIG_DIRECTORY` | calibre's config folder | `~/.config/calibre`, `~/Library/Preferences/calibre` or `%APPDATA%\calibre` |

For a flatpak calibre, set them with
`flatpak override --user --env=NAME=value com.calibre_ebook.calibre`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `--seed`/`--refresh` says **BLOCKED, rt=i** | A challenge didn't clear in 60 s. Retry once; if it persists, run `economist_setup.py` again. The page is saved as `last-challenge.html` in the profile. |
| **BLOCKED, rt=c**, or `DataDome captcha` in the download log | Log in again in your browser (solve the captcha), then run `economist_setup.py` again. Wait between attempts; don't retry in a loop. |
| `DataDome interstitial on … - retry N` in the log | Normal. The recipe waits for the challenge to clear. Only a bare `HTTP Error 403` at the end means it did not clear within about a minute. |
| `the copied request has no __cf_bm cookie` | You copied a stale row. Clear the Network log, reload twice, copy the new top row. |
| `that is "Copy as cURL (cmd)"` | On Windows, use **Copy as cURL (bash)**. |
| `USER_AGENT missing or still a placeholder` | The session was never imported. Run `economist_setup.py`. |
| `… is group/world readable` | `chmod 600` the cookie file. |
| `ERROR: chromium exited early` | The dedicated profile is already open in another browser window, or `ECONOMIST_BROWSER_CMD` is wrong. |
| `CDP transport unavailable`, or every article 403s | calibre couldn't find or run the helper. Check the path in `<calibre config>/economist-session.path`, run `--seed` again from the checkout, or set `ECONOMIST_SESSION_SCRIPT`. |
| Many articles fail with `IndexError: list index out of range` | The fetched page had no `__NEXT_DATA__`, usually a wrong `__cf_bm`. Run `economist_setup.py` again. |
| `Portal call failed: … ServiceUnknown` | Flatpak calibre lacks the host permission; run `economist_setup.py` and accept the offer, or `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre`. |
| `Failed to change to directory "/tmp/calibre-…"` | Should not happen with this recipe; please report it. |
| `NoArticles` | The site's page layout changed; the parser needs an update. |

Your session is tied to your IP and your browser's User-Agent. Set it up on the
machine that will download, and run `--seed` again after a network change.

## Security

The cookie file in calibre's config folder is your login. Nothing here prints
cookie values. On Linux and macOS the cookie file is created `0600` and the
profile `0700`. On Windows only your user account's permissions protect them. The
browser's DevTools port listens on localhost only and refuses connections from
web pages.

## Files

| File | Purpose |
|---|---|
| `economist.recipe` | The recipe you paste into calibre |
| `economist_setup.py` | One-time setup wizard |
| `economist_session.py` | Session helper: `--seed`, `--refresh`, `--serve`, `--stop`, `--status` |
| `economist_chrome.py` | Drives the browser over DevTools, with a minimal stdlib WebSocket client so there is nothing to install |
| `import_curl_cookies.py` | Imports the "Copy as cURL" export |
| `check_economist_access.py` | Diagnostic: checks whether a served session reaches the site |
| `test_*.py`, `conftest.py` | Unit tests (147). `conftest.py` loads the recipe with calibre's imports stubbed |
| `scripts/gates.sh` | The merge gates: `ruff`, `bandit`, `pytest`. CI runs exactly this script |
| `requirements-dev.txt` | Hash-locked dev tools (pytest, ruff, bandit). Not needed at run time |
| `docs/` | Design notes and history, and the manual setup |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.txt
PATH=.venv/bin:$PATH scripts/gates.sh
```

On Windows, `requirements-dev.txt` can't be used: it was generated on Linux and
lacks `colorama`, which pytest needs on Windows. Install the same tool versions
from `requirements-dev.in` instead: `uv pip install -r requirements-dev.in`.

CI (`.github/workflows/ci.yml`) runs the same script plus a gitleaks scan of the
full history on every push and pull request, and is a required check on `main`.
Commits are SSH-signed. Runtime code has no third-party dependencies; Dependabot
watches the dev tools and the pinned actions.

## Attribution and license

Recipe parsing code descends from calibre's built-in `economist.recipe` by Kovid
Goyal and unkn0wn. The session and CDP layers were developed with the assistance of
[Claude Code](https://claude.com/claude-code).

GPLv3, like calibre's recipes. Full text in `LICENSE`; the copyright notice and
upstream attribution are in `COPYRIGHT`.
