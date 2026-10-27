# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semver.

## [Unreleased]

### Added
- macOS support, contributed by @jordanhong (#9) and verified end to end on macOS 15.7.9 (Apple Silicon), calibre 9.14.0 and Google Chrome 153. calibre's config dir resolves to `~/Library/Preferences/calibre`; the default browser is an installed Google Chrome or Chromium; `--stop`'s pid fallback checks the process through `/bin/ps` because macOS has no `/proc`.
- Windows support, contributed by @Alectoid (#15). calibre's config dir resolves to `%APPDATA%\calibre`; the default browser is Google Chrome or Chromium from Program Files or `%LOCALAPPDATA%`; the dedicated profile defaults to `%LOCALAPPDATA%\calibre-economist-cdp\chrome-profile`. The POSIX permission checks on the cookie file and the session pointer apply on POSIX only, since Windows reports every writable file as `0666`. The recipe runs the helper with the `.venv` Python beside it, then the `py` launcher, then `python` on `PATH` (`ECONOMIST_PYTHON` overrides), and starts it without a console window. `--stop`'s pid fallback reads the command line through CIM. `import_curl_cookies.py` accepts a BOM and CRLF line endings and rejects Chrome's "Copy as cURL (cmd)" flavour with a pointer to the bash one. Tests that assert permission bits are skipped on Windows. Verified end to end on 2026-10-21: Windows 11 Pro, calibre 9.14, Google Chrome 155, Python 3.13 in a uv virtual environment.
- `economist_setup.py`, an interactive one-time setup: checks Python, the browser and calibre's config dir (and offers the flatpak host permission), reads the "Copy as cURL" export straight from the clipboard so no temporary file holds the login, lets a bad copy be retried, seeds the profile, and puts the recipe on the clipboard for calibre. `--from-file` covers machines without a clipboard tool. Verified on Windows alongside the port.

### Fixed
- Windows, after #15: `--stop`'s pid fallback reads PowerShell's output as UTF-8. PowerShell pipes it in the console code page and Python decoded it with the ANSI one, so a profile under a user name with non-ASCII letters (`C:\Users\Jürgen`) never matched, or the decode raised.
- Windows, after #15: the recipe tries the `py` launcher before `python` on `PATH`. A fresh Windows has a `python.exe` there that only opens the Microsoft Store, so `--serve` failed without saying why.
- `economist_setup.py --from-file` now offers to shred the export file when it fails to parse too, as the importer does, instead of leaving it behind.
- A DataDome interstitial (`403`, body `'rt':'i'`) on an in-page `fetch()` no longer kills the download. The page's DataDome tag solves it by itself but does not replay our request (`replayAfterChallenge: false`), so the first 403 on the index fetch was fatal. `CDPBrowser` now backs off and retries (2, 4, 8, 15 s), then re-navigates the tab to the index once and retries a last time; at most six fetches in about a minute. Seen 2026-09-11 and 2026-09-19.
- A DataDome captcha (`'rt':'c'`) now fails immediately with a "log in again, then `--seed`" message instead of a bare `HTTP Error 403`.

### Changed
- README cut down to setup and use (450 lines to about 210), built around `economist_setup.py`. The background, how it works, the design notes and traps, the known limitations, the runtime state and the macOS, Windows and flatpak notes move to `docs/design.md`, unchanged apart from links; the manual setup steps, the flatpak grant and the recipe install details to `docs/manual-setup.md`.
- README states plainly that Linux is the only supported platform and adds a *Porting to macOS and Windows* section naming the exact blockers found by an audit, so the work is pickup-able by someone who can test on those platforms.
- `ECONOMIST_BROWSER_CMD` is parsed with `shlex.split()` instead of being split on whitespace, so a quoted executable path with spaces works (#9). An empty value now falls back to the default browser instead of crashing.
- README after #15: Windows marked as verified by its author, like macOS; the setup tool's section warns that a clipboard history (Windows Win+V and its device sync) keeps the cURL export with the login.
- README: Requirements now lists Linux or macOS; the porting section becomes a *macOS* section (what differs, where it was verified) and a *Porting to Windows* section.

## [1.1.0] - 2026-09-09

### Removed
- **M4** The legacy QtWebEngine navigator (`--navigate-qt`, `--export`) and its Qt profile, and the mechanize and QtWebEngine fallback transports in the recipe. The CDP session is the only transport; a missing session now fails immediately instead of silently downloading error pages. `check_economist_access.py` probes the served session rather than the removed transports.

### Added
- **M3** The recipe is now tested: `conftest.py` loads `economist.recipe` as a module with calibre's imports stubbed, and 34 tests cover the CDP browser shim, the fetch contract, the session-script lookup and the JSON-to-HTML article parser. Index parsing still needs a live download; that gap is named in `conftest.py`.
- **M2** CI workflow (ruff, bandit, pytest, gitleaks over full history) with SHA-pinned actions; `scripts/gates.sh` runs the same gates locally; hash-locked `requirements-dev.txt`; Dependabot for actions and pip; `main` protected by a ruleset requiring the CI check; commits SSH-signed.

### Changed
- **L4** `LICENSE` now carries the full GPLv3 text so the license is machine-detectable; the copyright notice moved to `COPYRIGHT`.
- **L6** Shebangs normalised to `python3` and the executable bit set consistently on the runnable scripts.
- **L7** README carries a `Last updated` stamp and live CI, license and release badges.

### Fixed
- The recipe is now linted (ruff `extend-include`), which it never was. That found a dead `calibre.browser` import and a missing scheme check: an asset URL taken from page HTML reached `urllib.urlopen`, so a `file://` link in an article could pull a local file into the book. Non-http(s) asset URLs are now refused.
- The `--serve` reaper polls instead of sleeping the whole cap, so a normal `--stop` lets it exit within 30s rather than idling for 45 minutes.
- **L1** `--stop` verifies via `/proc/<pid>/cmdline` that the recorded pid is still our browser on our profile before signalling it; pids are recycled.
- **L2** The session-pointer file, whose contents are imported and executed inside calibre, is written 0644 and refused by the recipe if it is group- or world-writable.
- Endpoint-location test asserted a flatpak-specific path and failed on any machine without the flatpak calibre (caught by the new CI). It now asserts the real invariant: the file lives in calibre's config dir and never under the browser's profile.
- **H1** The DevTools port no longer passes `--remote-allow-origins=*`, which let any web page open on the machine connect and read the logged-in session. Origin-bearing clients are now refused by Chromium (verified: 403 on the handshake); the recipe's client sends no Origin and still connects.
- **H1** `--serve` starts a detached reaper that closes the served browser after `ECONOMIST_SERVE_MAX_S` (default 45 min), so a crashed download cannot leave the port open indefinitely.
- **L3** Every best-effort exception handler now logs the exception at debug level with a written justification for the broad catch, instead of swallowing it silently. Set `ECONOMIST_DEBUG=1` to see them.
- **M1** TLS certificate verification is no longer disabled on the WebEngine fallback transport in the recipe and in `check_economist_access.py`.

## [1.0.0] - 2026-09-09

### Added
- First public release: recipe fetching through the user's own Chromium over the DevTools Protocol, session helpers, cookie importer, diagnostic, unit tests, README.
