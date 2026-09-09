# The Economist for calibre, through your own Chromium

A working replacement for calibre's built-in **The Economist** news recipe, which has
been blocked since late August 2026. It downloads a full weekly edition, with
images, automatically and on a schedule, by letting calibre fetch through a real
Chromium that you already have installed, driven over the Chrome DevTools
Protocol (CDP).

**Status:** working. Last verified 2026-09-09 (edition of 2026-09-05), 0 failures,
0 HTTP 403s. Discussion: MobileRead thread
[Economist failed after upgrade to 9.10](https://www.mobileread.com/forums/showthread.php?t=374136).

You need an Economist **subscription**. This project shares code only; no
downloaded editions are or will be distributed.

## Contents

- [Why the built-in recipe is dead](#why-the-built-in-recipe-is-dead)
- [How this works](#how-this-works)
- [Requirements](#requirements)
- [Setup](#setup)
- [Installing the recipe into calibre](#installing-the-recipe-into-calibre)
- [Everyday use](#everyday-use)
- [Design notes and traps](#design-notes-and-traps)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Files](#files)
- [Platform notes: flatpak and Fedora Linux](#platform-notes-flatpak-and-fedora-linux)
- [Attribution and license](#attribution-and-license)

## Why the built-in recipe is dead

economist.com sits behind **two** bot walls:

| Request | Response |
|---|---|
| no cookies | `403`, `cf-mitigated: challenge`, `_cf_chl_opt` — **Cloudflare** managed challenge |
| valid Cloudflare cookies | `403`, `x-datadome: protected`, `x-dd-b`, body `rt:'i'` — **DataDome** interstitial |

Both bind their clearance to the TLS and JavaScript fingerprint of the client
that earned it. That single fact kills every cheap fix:

| Attempt | Result |
|---|---|
| 8 different User-Agents (Firefox 124–144, Chrome 131, Opera 110, Safari 17) via `calibre.browser()` | all 403 |
| `curl` with a complete browser header set (`Accept`, `Accept-Language`, `Sec-Fetch-*`, `Upgrade-Insecure-Requests`) | 403 |
| `curl` **with freshly earned cookies** | 200 — but calibre's mechanize gets **403 on the same URLs**. Python's TLS handshake itself is rejected |
| copying cookies out of a browser | `__cf_bm` lives 30 minutes and is reissued on every response; an export is a snapshot of a self-renewing value |
| calibre's `browser_type = 'webengine'` | returns the challenge page, never the article. The scraper backend never *navigates* to the target (it loads a stub page and runs an in-page downloader), so the challenge JavaScript never executes |
| a spoofed User-Agent on calibre's bundled Chromium 134 | DataDome requests `Sec-CH-UA` client hints, which the real engine answers honestly. The contradiction gets the device reclassified |
| upstream's GraphQL fallback | its host `cp2-graphql-gateway.p.aws.economist.com` no longer resolves |

Kovid Goyal's assessment in the thread above is that the block is statistical
fingerprinting and that "the only way to fix this robustly is to use a browser
engine that is impossible to statistically isolate from regular browsers". This
project agrees, and observes that you do not have to bundle such an engine. You
already have one.

## How this works

```
one-time:  browser export ──▶ import_curl_cookies.py ──▶ economist_session.py --seed
                                                                   │
every run: recipe ──▶ economist_session.py --serve ──▶ your Chromium stays up on a
                             │                          dedicated profile, publishes
                             │                          its DevTools endpoint
                             └──▶ CDPBrowser ───────────────────┘
                                  every article and image fetched with an
                                  in-page fetch() in that session
```

1. **A dedicated, persistent Chromium profile** is seeded once with your login
   cookies (the long-lived `fcx_*` ones). It is deliberately *not* seeded with
   `datadome`, `__cf_bm`, `_cfuvid` or `cf_clearance`: those identify a device,
   and copying them from a profile that has been blocked imports the block.
2. **Each run, `economist_session.py --serve`** starts Chromium on that profile
   with `--remote-debugging-port`, navigates to `/weeklyedition`, and **polls for
   up to 60 s** so a DataDome interstitial has time to solve itself and reload.
   Cookies are read back with `Network.getAllCookies`, decrypted and including
   HttpOnly, so there is no SQLite decryption and no commit race. If `__cf_bm` is
   still under 20 minutes old the navigation is skipped entirely.
3. **The recipe's `get_browser()` returns a `CDPBrowser`** that mirrors the
   surface calibre's fetcher uses (`open`, `open_novisit`, `clone_browser`,
   `shutdown`) and fetches every article and image with an in-page `fetch()`
   inside the live page. Same TLS, same fingerprint, same cookie jar, same origin.
   calibre's QtWebEngine is not involved anywhere. Index parsing (`__NEXT_DATA__`)
   is the same as the built-in recipe.

The fingerprint is honest because it *is* a real browser, and the User-Agent is
taken from `Browser.getVersion`, so it can never drift from the engine again.

> **The cookies are credentials.** They live in a `chmod 600` file in calibre's
> config directory and a `0700` profile directory. Nothing here ever prints a
> cookie value, only names and lengths.

## Requirements

- An Economist subscription.
- calibre 8 or 9 (tested on 9.7, flatpak).
- A Chrome or Chromium. Default is the `io.github.ungoogled_software.ungoogled_chromium`
  flatpak; any other is a one-line environment override (see Setup).
- Python 3.10+ on the host for the helper scripts. **No third-party packages**:
  the CDP client and its WebSocket implementation are stdlib only.
- Linux. The CDP part is portable; the process-launching glue (flatpak-spawn,
  paths) has only been built for Linux. Ports welcome.

## Setup

One time: harvest a browser export, import it, seed the profile.

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

6. Import and seed, on the **host** (not inside a flatpak):

   ```bash
   git clone https://github.com/CR0CKER/calibre-economist-cdp ~/calibre-economist-cdp
   cd ~/calibre-economist-cdp
   wl-paste > /run/user/$(id -u)/eco-curl.txt
   python3 import_curl_cookies.py /run/user/$(id -u)/eco-curl.txt
   python3 economist_session.py --seed
   ```

   `--seed` launches the browser, navigates, waits for the site to accept the
   session, and writes the renewed cookie file. It also records its own location
   in calibre's config dir so the recipe can find it. If the helper reports
   `BLOCKED`, see Troubleshooting.

**Using a different browser or profile location:** set these in the environment
that runs calibre (for a flatpak calibre: `flatpak override --user --env=... com.calibre_ebook.calibre`):

| Variable | Meaning | Default |
|---|---|---|
| `ECONOMIST_BROWSER_CMD` | command that starts Chromium, e.g. `google-chrome` or `flatpak run --command=chromium org.chromium.Chromium` | ungoogled-chromium flatpak |
| `ECONOMIST_CHROME_PROFILE` | the dedicated profile directory (never your everyday profile) | inside the flatpak's config tree |
| `ECONOMIST_SESSION_SCRIPT` | path to `economist_session.py`, if the recorded location is wrong | the recorded location |
| `CALIBRE_CONFIG_DIRECTORY` | calibre's config dir, if not the flatpak or `~/.config/calibre` default | auto |

**Flatpak calibre only:** the recipe has to start a program on the host from inside
calibre's sandbox. Grant that once:

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

Editing `economist_session.py` or `economist_chrome.py` needs no reinstall. The
recipe reads them from disk at run time.

## Everyday use

Download the recipe in calibre as usual; it refreshes the session itself,
so scheduled unattended downloads work.

To inspect or refresh by hand:

```bash
python3 economist_session.py --status
python3 economist_session.py --refresh
python3 economist_session.py --stop      # shut down a browser left running by a download
```

`--refresh` exits `0` when the session works. On failure it reports which kind of
DataDome response it got and saves the page as `last-challenge.html` in the profile:

| Reported | Meaning |
|---|---|
| `rt=i` (interstitial) | A soft challenge that did not resolve in 60 s. Retry; if it persists, re-seed. |
| `rt=c` (hard block) | The device is burned. Log in again in the browser and re-run `--seed`. |

Routine cookie expiry needs no intervention. The login cookies persist for months.

## Design notes and traps

Every one of these caused a silent failure during development.

| Trap | Consequence |
|---|---|
| **Lying about the User-Agent** (claiming Chrome 151 from a Chromium 134 engine) | `Sec-CH-UA` client hints report the real version. DataDome sees the contradiction and reclassifies the device. Take the UA *from the browser* (`Browser.getVersion`) |
| Sampling the page **once**, shortly after load | A DataDome interstitial takes seconds and reloads itself, so a single early sample only ever sees the challenge. Poll to a timeout |
| Seeding `datadome` / `__cf_bm` into a fresh profile | Those identify a *device*. Copying them from a blocked profile imports the block. Seed the login cookies only |
| Reading Chromium's `Cookies` SQLite file directly | Values are encrypted with OSCrypt. `Network.getAllCookies` returns them decrypted, HttpOnly included |
| `flatpak-spawn --host` **without `--directory=`** | The sandbox's CWD during a fetch is a private `/tmp/calibre-*` path that does not exist on the host; the portal refuses: `Failed to change to directory …` |
| Putting the CDP endpoint file under the **browser's** `~/.var/app/…` tree | flatpak reserves `~/.var/app`; an app with `filesystems=host` still sees only its *own* subtree there. The recipe silently falls back to mechanize and every article 403s. It lives in calibre's config dir instead |
| An in-page `fetch()` to a **cross-origin** URL | Blocked by CORS (`TypeError: Failed to fetch`). Non-economist.com URLs (the masthead) go through plain `urllib`; they need no credentials |
| Using `curl` to decide whether mechanize will work | curl 200, mechanize 403, same cookies, same URLs. Not a valid proxy |
| Selecting cookies with `host_key LIKE '%economist.com'` | `.marber-cdn.economist.com` and `p.zephr.economist.com` carry **their own `__cf_bm`**. Symptom: most articles die with `IndexError` while the index works. Use the exact hosts |
| `--headless` | Is itself a bot signal. The browser runs headful, positioned off-screen and minimized, with background throttling disabled |
| `browser_type = 'webengine'` set only inside `get_content` | Index works, every article 403s: articles and images go through `self.browser` in calibre's own fetcher |
| Running `ebook-convert` from a path a flatpak calibre cannot see (`/tmp/…`) | calibre falls back to a builtin-title lookup and reports a misleading `TypeError: 'NoneType' object is not subscriptable` |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `--refresh` says **BLOCKED, rt=i** | Interstitial did not clear in 60 s. Retry once; if it persists, redo Setup. Inspect `last-challenge.html` in the profile. |
| `--refresh` says **BLOCKED, rt=c** | Hard block. Log in again in the browser, then `--seed`. |
| `Portal call failed: … ServiceUnknown` | Flatpak calibre lacks the host-spawn grant; see Setup. |
| `Failed to change to directory "/tmp/calibre-…"` | `flatpak-spawn` called without `--directory=`. Should not happen with this recipe; report it. |
| `ERROR: chromium exited early` | Another Chromium is already using that profile directory, or the browser command is wrong (`ECONOMIST_BROWSER_CMD`). |
| `the copied request has no __cf_bm cookie` | You copied a stale row from the Network log. Clear it, reload **twice**, copy the new top document row. |
| `USER_AGENT missing or still a placeholder` | The cookie file was never imported. Run `import_curl_cookies.py`. |
| `… is group/world readable` | `chmod 600` the cookie file. |
| Every article 403s, log says `CDP transport unavailable` or nothing about `session:` | The recipe could not find or run `economist_session.py`. Check the recorded path in `<calibre config>/economist-session.path` or set `ECONOMIST_SESSION_SCRIPT`. |
| Many articles fail with `IndexError: list index out of range` | Fetched page had no `__NEXT_DATA__`, usually a wrong `__cf_bm` (host trap above). |
| `NoArticles` | The index parsed but was empty: the site's `__NEXT_DATA__` layout changed and the parser needs updating. |

## Known limitations

- **mechanize can never work.** Clearance is bound to the TLS fingerprint. Only a
  real browser's handshake is accepted.
- **Fetches are serialised.** One websocket means one CDP call at a time. Each fetch
  is a few tenths of a second, so a full edition takes a few minutes.
- **The session is IP- and User-Agent-bound.** Seed on the machine that will
  download, and re-seed after a network change.
- **A bot-management vendor can reclassify the device at any time.** This design
  makes that *legible* (the failing page is saved, the DataDome response type is
  reported) and recoverable with a re-seed. It is not a guarantee, and if enough
  people run the same setup the statistics may catch up with it too. Each user
  driving their own real browser profile is the best defence available.
- A handful of items are legitimately short: cartoons, photo pieces, the indicators
  table, and `/interactive/` articles, which the recipe replaces with a
  browser-only notice, as upstream does.

## Files

| Path | Purpose |
|---|---|
| `economist.recipe` | The recipe. Paste into calibre as a custom news source. |
| `economist_session.py` | Entry point: freshness logic, cookie-file contract, reporting, config-dir discovery. |
| `economist_chrome.py` | The navigator and fetch session: drives Chromium over CDP; `--serve` leaves it running. Contains a minimal RFC 6455 WebSocket client so there is nothing to install. |
| `import_curl_cookies.py` | One-time import from a DevTools "Copy as cURL". |
| `check_economist_access.py` | Standalone diagnostic: can these cookies reach the site? Runs under `calibre-debug`. |
| `test_*.py` | Unit tests (63). `python -m pytest -q` |

Runtime state, all outside the repository:

| Path | Purpose |
|---|---|
| `<calibre config>/economist_cookies.txt` | Credentials, `0600`. |
| `<calibre config>/economist-session.path` | Where the recipe finds the helper. |
| `<calibre config>/economist-cdp-endpoint.json` | The live DevTools endpoint while a download runs. |
| `<profile dir>/` | The dedicated Chromium profile, `0700`. |

## Platform notes: flatpak and Fedora Linux

This was built on Fedora (aarch64) with calibre and Chromium both as
flatpaks. Two things there are worth knowing even if you are elsewhere:

- On that machine calibre's own QtWebEngine worker (`calibre-parallel`) segfaults
  inside `libQt6WebEngineCore`, and worse than crashing it *hangs* the download job.
  That is why this design keeps QtWebEngine out of the fetch path entirely rather
  than using `browser_type = 'webengine'` with replayed cookies, which would
  otherwise be a viable (if fragile) alternative.
- A flatpak override with `--disable-gpu` for calibre, added to stop those
  crashes, removed WebGL from calibre's Chromium and was one of the two signals
  that got the device reclassified by DataDome. Driving an external browser
  sidesteps this too.

## Attribution and license

Recipe parsing code descends from calibre's built-in `economist.recipe` by Kovid
Goyal and unkn0wn. The session and CDP layers were developed with the assistance of
[Claude Code](https://claude.com/claude-code).

GPLv3, like calibre's recipes. See `LICENSE`.
