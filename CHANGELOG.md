# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semver.

## [Unreleased]

### Added
- **M2** CI workflow (ruff, bandit, pytest, gitleaks over full history) with SHA-pinned actions; `scripts/gates.sh` runs the same gates locally; hash-locked `requirements-dev.txt`; Dependabot for actions and pip; `main` protected by a ruleset requiring the CI check; commits SSH-signed.

### Changed
- **L4** `LICENSE` now carries the full GPLv3 text so the license is machine-detectable; the copyright notice moved to `COPYRIGHT`.
- **L6** Shebangs normalised to `python3` and the executable bit set consistently on the runnable scripts.
- **L7** README carries a `Last updated` stamp and live CI, license and release badges.

### Fixed
- Endpoint-location test asserted a flatpak-specific path and failed on any machine without the flatpak calibre (caught by the new CI). It now asserts the real invariant: the file lives in calibre's config dir and never under the browser's profile.
- **H1** The DevTools port no longer passes `--remote-allow-origins=*`, which let any web page open on the machine connect and read the logged-in session. Origin-bearing clients are now refused by Chromium (verified: 403 on the handshake); the recipe's client sends no Origin and still connects.
- **H1** `--serve` starts a detached reaper that closes the served browser after `ECONOMIST_SERVE_MAX_S` (default 45 min), so a crashed download cannot leave the port open indefinitely.
- **L3** (partial) Swallowed exceptions on the best-effort shutdown and probe paths are now logged at debug level.
- **M1** TLS certificate verification is no longer disabled on the WebEngine fallback transport in the recipe and in `check_economist_access.py`.

## [1.0.0] - 2026-09-09

### Added
- First public release: recipe fetching through the user's own Chromium over the DevTools Protocol, session helpers, cookie importer, diagnostic, unit tests, README.
