# Design notes

Why calibre's built-in Economist recipe stopped working, how this project gets
around it, the traps found along the way, and what differs per platform. For
setup and everyday use, see the [README](../README.md).

## Contents

- [Why the built-in recipe is dead](#why-the-built-in-recipe-is-dead)
- [How this works](#how-this-works)
- [Design notes and traps](#design-notes-and-traps)
- [Known limitations](#known-limitations)
- [Runtime state](#runtime-state)
- [macOS](#macos)
- [Windows](#windows)
- [Platform notes: flatpak and Fedora Linux](#platform-notes-flatpak-and-fedora-linux)

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

Kovid Goyal's assessment in the
[MobileRead thread](https://www.mobileread.com/forums/showthread.php?t=374136) is
that the block is statistical fingerprinting and that "the only way to fix this
robustly is to use a browser engine that is impossible to statistically isolate
from regular browsers". This project agrees, and observes that you do not have
to bundle such an engine. You already have one.

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
4. **A DataDome interstitial on a fetch is ridden out, not fatal.** DataDome
   sometimes answers an in-page `fetch()` with `403` and a body carrying
   `'rt':'i'`. The page's own DataDome tag solves that within seconds, but it is
   configured with `replayAfterChallenge: false`, so it never retries *our*
   request. `CDPBrowser` therefore backs off and retries (2, 4, 8, 15 s), then
   navigates the tab to `/weeklyedition` once and retries a last time: at most
   six fetches in about a minute. A captcha (`'rt':'c'`) needs a human, so it
   fails at once with a re-login hint. Detect challenges by the body's `rt`, not
   by headers: `x-datadome: protected` is present on healthy `200`s too.

The fingerprint is honest because it *is* a real browser, and the User-Agent is
taken from `Browser.getVersion`, so it can never drift from the engine again.

> **The cookies are credentials.** They live in a `chmod 600` file in calibre's
> config directory and a `0700` profile directory. Nothing here ever prints a
> cookie value, only names and lengths.

## Design notes and traps

Every one of these caused a silent failure during development.

| Trap | Consequence |
|---|---|
| **Lying about the User-Agent** (claiming Chrome 151 from a Chromium 134 engine) | `Sec-CH-UA` client hints report the real version. DataDome sees the contradiction and reclassifies the device. Take the UA *from the browser* (`Browser.getVersion`) |
| Sampling the page **once**, shortly after load | A DataDome interstitial takes seconds and reloads itself, so a single early sample only ever sees the challenge. Poll to a timeout |
| Seeding `datadome` / `__cf_bm` into a fresh profile | Those identify a *device*. Copying them from a blocked profile imports the block. Seed the login cookies only |
| Reading Chromium's `Cookies` SQLite file directly | Values are encrypted with OSCrypt. `Network.getAllCookies` returns them decrypted, HttpOnly included |
| `flatpak-spawn --host` **without `--directory=`** | The sandbox's CWD during a fetch is a private `/tmp/calibre-*` path that does not exist on the host; the portal refuses: `Failed to change to directory …` |
| Putting the CDP endpoint file under the **browser's** `~/.var/app/…` tree | flatpak reserves `~/.var/app`; an app with `filesystems=host` still sees only its *own* subtree there, so the recipe cannot find the endpoint and the download fails outright. It lives in calibre's config dir instead |
| An in-page `fetch()` to a **cross-origin** URL | Blocked by CORS (`TypeError: Failed to fetch`). Non-economist.com URLs (the masthead) go through plain `urllib`; they need no credentials |
| Using `curl` to decide whether mechanize will work | curl 200, mechanize 403, same cookies, same URLs. Not a valid proxy |
| Selecting cookies with `host_key LIKE '%economist.com'` | `.marber-cdn.economist.com` and `p.zephr.economist.com` carry **their own `__cf_bm`**. Symptom: most articles die with `IndexError` while the index works. Use the exact hosts |
| `--headless` | Is itself a bot signal. The browser runs headful, positioned off-screen and minimized, with background throttling disabled |
| `--remote-allow-origins=*` on the DevTools port | Lets **any web page open in any browser on the machine** connect to the port and read the logged-in session's cookies. Chromium admits origin-less clients (this one sends no `Origin`) and rejects browser-originated ones without the flag, so it is simply omitted. A reaper also closes a served browser after `ECONOMIST_SERVE_MAX_S` |
| `browser_type = 'webengine'` set only inside `get_content` | Index works, every article 403s: articles and images go through `self.browser` in calibre's own fetcher |
| Running `ebook-convert` from a path a flatpak calibre cannot see (`/tmp/…`) | calibre falls back to a builtin-title lookup and reports a misleading `TypeError: 'NoneType' object is not subscriptable` |

## Known limitations

- **mechanize can never work, so there is no fallback transport.** Clearance is
  bound to the TLS fingerprint; only a real browser's handshake is accepted. If
  the served session is missing the download fails immediately and says so,
  rather than silently producing an edition of error pages.
- **Request rate matters to DataDome.** It scores patterns, not just
  fingerprints. One download at a time, with hours between attempts, is fine;
  bursts of fresh sessions hammering the index are not, and can earn the IP a
  temporary captcha block.
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

## Runtime state

All outside the repository:

| Path | Purpose |
|---|---|
| `<calibre config>/economist_cookies.txt` | Credentials, `0600`. |
| `<calibre config>/economist-session.path` | Where the recipe finds the helper. |
| `<calibre config>/economist-cdp-endpoint.json` | The live DevTools endpoint while a download runs. |
| `<profile dir>/` | The dedicated Chromium profile, `0700`. |

## macOS

Contributed by [@jordanhong](https://github.com/jordanhong) in
[#9](https://github.com/CR0CKER/calibre-economist-cdp/pull/9). I have no Mac, so
this rests on the contributor's test and on the unit tests, which run on Linux.

**Verified on:** macOS 15.7.9 on Apple Silicon (`arm64`), calibre 9.14.0, Google
Chrome 153.0.8010.48, Python 3.13.5: a full end-to-end edition download.

What differs from Linux, all selected automatically by `sys.platform == 'darwin'`:

| Where | On macOS |
|---|---|
| calibre's config dir | `~/Library/Preferences/calibre`, calibre's native location. `CALIBRE_CONFIG_DIRECTORY` still wins |
| `economist_chrome.py`, default browser | The first of `/Applications/Google Chrome.app`, `/Applications/Chromium.app`, then `google-chrome`, `chromium` or `chromium-browser` on `PATH`. `ECONOMIST_BROWSER_CMD` still wins |
| `economist_chrome.py`, `ECONOMIST_BROWSER_CMD` | Parsed with `shlex.split()` on every platform, so quote a path with spaces: `ECONOMIST_BROWSER_CMD='"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"'` |
| `economist_chrome.py`, `pid_is_our_browser` | macOS has no `/proc`, so `/bin/ps -p <pid> -o command=` supplies the command line for the same profile-path check before the crash-only pid fallback. The normal shutdown still goes over CDP |
| Setup | `pbpaste` instead of `wl-paste`, and the cURL copy goes to the per-user `$TMPDIR` (see [manual setup](manual-setup.md#the-steps) step 6) |

The default profile directory is still the flatpak-shaped
`~/.var/app/io.github.ungoogled_software.ungoogled_chromium/config/chromium-economist`.
It works on macOS, the browser just creates it; set `ECONOMIST_CHROME_PROFILE`
if you would rather keep it somewhere else.

<sub>[↑ Back to contents](#contents)</sub>

## Windows

**Verified by its author**
([#15](https://github.com/CR0CKER/calibre-economist-cdp/pull/15)) on Windows 11
Pro, calibre 9.14, Google Chrome 155, Python 3.13 in a uv virtual environment:
a full end-to-end edition download, set up with `economist_setup.py`. The unit tests pass on Windows; those that assert POSIX
permission bits are skipped there. For development on Windows, see the
[README](../README.md#development).

What differs, all selected automatically by `sys.platform == 'win32'`:

| Where | On Windows |
|---|---|
| calibre's config dir | `%APPDATA%\calibre`, calibre's native location. Inside calibre the recipe asks calibre itself |
| Default browser | `chrome.exe` of Google Chrome or Chromium under Program Files, Program Files (x86) or `%LOCALAPPDATA%`, then `chrome` or `chromium` on `PATH` |
| Default profile | `%LOCALAPPDATA%\calibre-economist-cdp\chrome-profile` |
| `ECONOMIST_BROWSER_CMD` | Parsed in Windows mode, so backslashes survive; quote a path with spaces |
| Python for the helper | `.venv\Scripts\python.exe` beside `economist_session.py`, then the `py` launcher, then `python` on `PATH` (the `py` launcher comes first because a fresh Windows has a `python.exe` that only opens the Microsoft Store); `ECONOMIST_PYTHON` overrides. calibre's own interpreter is not usable for this |
| Console windows | The helper and its watchdog are started with `CREATE_NO_WINDOW`, so a scheduled download does not flash a console |
| `--stop`'s pid fallback | No `/proc` or `ps`: the command line comes from `Get-CimInstance Win32_Process` through PowerShell, for the same profile-path check, read as UTF-8 so a user name with non-ASCII letters still matches |
| Permission checks | **Off.** Windows has no POSIX permission bits and reports every writable file as `0666`, so the cookie-file and session-pointer checks would refuse everything. The credential files are protected only by the ACL of your user profile, where they live |
| Setup | "Copy as cURL (**bash**)", `Get-Clipboard` instead of `wl-paste` (see [manual setup](manual-setup.md#the-steps) step 6). The importer accepts the BOM and CRLF a Windows paste brings, and rejects the (cmd) flavour |

<sub>[↑ Back to contents](#contents)</sub>

## Platform notes: flatpak and Fedora Linux

This was built on Fedora (aarch64) with calibre and Chromium both as
flatpaks. Two things there are worth knowing even if you are elsewhere:

- On that machine calibre's own QtWebEngine worker (`calibre-parallel`) segfaults
  inside `libQt6WebEngineCore`, and worse than crashing it *hangs* the download job.
  That is why this design keeps QtWebEngine out of the fetch path entirely rather
  than using `browser_type = 'webengine'` with replayed cookies.
- A flatpak override with `--disable-gpu` for calibre, added to stop those
  crashes, removed WebGL from calibre's Chromium and was one of the two signals
  that got the device reclassified by DataDome. Driving an external browser
  sidesteps this too.
