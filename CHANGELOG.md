# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semver.

## [Unreleased]

### Fixed
- **H1** The DevTools port no longer passes `--remote-allow-origins=*`, which let any web page open on the machine connect and read the logged-in session. Origin-bearing clients are now refused by Chromium (verified: 403 on the handshake); the recipe's client sends no Origin and still connects.
- **H1** `--serve` starts a detached reaper that closes the served browser after `ECONOMIST_SERVE_MAX_S` (default 45 min), so a crashed download cannot leave the port open indefinitely.
- **M1** TLS certificate verification is no longer disabled on the WebEngine fallback transport in the recipe and in `check_economist_access.py`.

## [1.0.0] - 2026-09-09

### Added
- First public release: recipe fetching through the user's own Chromium over the DevTools Protocol, session helpers, cookie importer, diagnostic, unit tests, README.
