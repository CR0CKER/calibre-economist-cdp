# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semver.

## [Unreleased]

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
