#!/usr/bin/env python3
"""Preflight check: can calibre reach economist.com with the harvested cookies?

economist.com sits behind a Cloudflare *managed* JS challenge plus DataDome. No
plain HTTP client can solve that challenge, and calibre's QtWebEngine scraper
cannot either (its backend never navigates to the target page, so the challenge
JS never runs). The only workable route is to replay a real browser's clearance
and session cookies.

Those cookies are short-lived and bound to IP + User-Agent, so this script exists
to answer "are my cookies still good?" in seconds, instead of finding out after a
fifteen-minute download fails.

Run it inside the flatpak::

    flatpak run --command=calibre-debug com.calibre_ebook.calibre \
        -e /path/to/calibre-economist-cdp/check_economist_access.py

Exit codes: 0 = at least one transport works, 1 = cookies stale/unusable,
2 = cookie file missing or malformed.

SECURITY: cookie values are credentials. This script prints cookie *names* and
value *lengths* only, never the values themselves.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from contextlib import suppress
from typing import NoReturn, TypedDict

INDEX_URL = 'https://www.economist.com/weeklyedition'
COOKIE_DOMAIN = '.economist.com'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from economist_session import COOKIE_FILE  # noqa: E402  same config-dir logic everywhere

# Markers proving we got an interstitial rather than the real page.
CHALLENGE_MARKERS: tuple[bytes, ...] = (
    b'captcha-delivery',      # DataDome
    b'Just a moment',         # Cloudflare managed challenge
    b'_cf_chl_opt',           # Cloudflare challenge payload
    b'Please enable JS',      # DataDome noscript text
)

# Proof we got the real Next.js page the recipe's parser needs.
SUCCESS_MARKER = b'__NEXT_DATA__'


class Credentials(TypedDict):
    """User-Agent and cookie pairs harvested together from one browser session."""

    user_agent: str
    cookies: list[tuple[str, str]]


class ProbeResult(TypedDict):
    """Outcome of fetching the index through one transport."""

    transport: str
    ok: bool
    detail: str


def fail_config(message: str) -> NoReturn:
    """Report a cookie-file problem on stderr and exit with the documented code 2."""
    print(f'ERROR: {message}', file=sys.stderr)
    raise SystemExit(2)


def parse_cookie_header(raw: str) -> list[tuple[str, str]]:
    """Split a verbatim ``Cookie:`` request-header value into name/value pairs.

    Values may legitimately contain ``=`` (base64 padding, JWTs), so split on the
    first ``=`` only. Pairs without ``=`` are skipped rather than guessed at.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(';'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        name, _, value = chunk.partition('=')
        name = name.strip()
        value = value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def load_credentials(path: str = COOKIE_FILE) -> Credentials:
    """Read and validate the cookie file. Raises SystemExit(2) on any problem."""
    if not os.path.exists(path):
        fail_config(
            f'cookie file not found: {path}\n'
            'Create it with two lines (see README.md):\n'
            '  USER_AGENT=<navigator.userAgent from the browser>\n'
            '  COOKIE=<verbatim Cookie: request header>\n'
            f'Then: chmod 600 {path}'
        )

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        fail_config(
            f'{path} is group/world accessible (mode {mode:04o}).\n'
            f'These cookies are credentials. Fix with: chmod 600 {path}'
        )

    user_agent = ''
    cookie_header = ''
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, _, value = line.partition('=')
            key = key.strip().upper()
            if key == 'USER_AGENT':
                user_agent = value.strip()
            elif key == 'COOKIE':
                cookie_header = value.strip()

    if not user_agent or user_agent.startswith('<'):
        fail_config(f'USER_AGENT missing or still a placeholder in {path}')
    if not cookie_header or cookie_header.startswith('<'):
        fail_config(f'COOKIE missing or still a placeholder in {path}')

    cookies = parse_cookie_header(cookie_header)
    if not cookies:
        fail_config(f'COOKIE line in {path} parsed to zero cookies')

    return {'user_agent': user_agent, 'cookies': cookies}


# Cloudflare's __cf_bm carries its issue time in the value and lives exactly 30
# minutes. It is the shortest-lived cookie in the jar, so it, not cf_clearance,
# is what actually bounds the usable window.
CF_BM_LIFETIME_MIN = 30.0
_CF_BM_ISSUED = re.compile(r'^[^-]*-(\d{10})\.')


def cf_bm_age_minutes(cookies: list[tuple[str, str]]) -> float | None:
    """Age of the __cf_bm cookie in minutes, or None if it is absent/unparseable."""
    for name, value in cookies:
        if name == '__cf_bm':
            m = _CF_BM_ISSUED.match(value)
            if m:
                return (time.time() - int(m.group(1))) / 60.0
    return None


def classify(raw: bytes) -> ProbeResult:
    """Decide whether a response body is the real page or an interstitial."""
    for marker in CHALLENGE_MARKERS:
        if marker in raw:
            return {
                'transport': '',
                'ok': False,
                'detail': f'blocked - challenge page ({marker.decode()}), {len(raw)} bytes',
            }
    if SUCCESS_MARKER in raw:
        return {'transport': '', 'ok': True, 'detail': f'OK - __NEXT_DATA__ present, {len(raw)} bytes'}
    return {
        'transport': '',
        'ok': False,
        'detail': f'unexpected page - no __NEXT_DATA__ and no challenge marker, {len(raw)} bytes',
    }


def probe_mechanize(creds: Credentials) -> ProbeResult:
    """Transport A: calibre's mechanize browser (Python TLS fingerprint)."""
    from calibre import browser

    br = browser(user_agent=creds['user_agent'])
    for name, value in creds['cookies']:
        br.set_simple_cookie(name, value, COOKIE_DOMAIN)
    raw = br.open_novisit(INDEX_URL, timeout=60).read()
    result = classify(raw)
    result['transport'] = 'mechanize'
    return result


def probe_webengine(creds: Credentials) -> ProbeResult:
    """Transport B: QtWebEngine (Chromium TLS fingerprint - closest to a real browser).

    Note WebEngineBrowser has no ``addheaders``, so cookies must go in via
    set_simple_cookie rather than a raw Cookie header.
    """
    from calibre.scraper.qt import WebEngineBrowser

    br = WebEngineBrowser(user_agent=creds['user_agent'])
    try:
        br.set_user_agent(creds['user_agent'])
        for name, value in creds['cookies']:
            br.set_simple_cookie(name, value, COOKIE_DOMAIN)
        raw = br.open_novisit(INDEX_URL, timeout=90).read()
    finally:
        # The browser spawns a worker process that keeps the interpreter alive.
        # Without this the script hangs after printing its result.
        with suppress(Exception):
            br.shutdown()
    result = classify(raw)
    result['transport'] = 'webengine'
    return result


def main() -> int:
    creds = load_credentials()

    print(f'Cookie file : {COOKIE_FILE}')
    print(f'User-Agent  : {creds["user_agent"]}')
    print(f'Cookies     : {len(creds["cookies"])} loaded (names and lengths only)', flush=True)
    for name, value in creds['cookies']:
        print(f'    {name:<28s} len={len(value)}')
    age = cf_bm_age_minutes(creds['cookies'])
    if age is None:
        print('Cookie age  : unknown (no parseable __cf_bm)')
    else:
        remaining = CF_BM_LIFETIME_MIN - age
        verdict = f'{remaining:.0f} min left' if remaining > 0 else 'EXPIRED - re-harvest'
        print(f'Cookie age  : {age:.1f} min of {CF_BM_LIFETIME_MIN:.0f} ({verdict})')
    interesting = {'cf_clearance', 'datadome'}
    present = {n for n, _ in creds['cookies']}
    for wanted in sorted(interesting):
        state = 'present' if wanted in present else 'MISSING'
        print(f'    -> {wanted}: {state}')
    print()

    results: list[ProbeResult] = []
    for label, probe in (('mechanize', probe_mechanize), ('webengine', probe_webengine)):
        try:
            result = probe(creds)
        except Exception as e:  # noqa: BLE001 - report any transport failure, keep probing
            result = {'transport': label, 'ok': False, 'detail': f'{type(e).__name__}: {e}'}
        results.append(result)
        status = 'PASS' if result['ok'] else 'FAIL'
        print(f'[{status}] {result["transport"]:<10s} {result["detail"]}', flush=True)

    winners = [r['transport'] for r in results if r['ok']]
    print()
    if winners:
        print(f'GO: usable transport(s): {", ".join(winners)}')
        print(f'Use browser transport "{winners[0]}" in the recipe.')
        return 0

    print('NO-GO: no transport reached the real page.')
    print('Either the cookies have expired (re-harvest them) or Cloudflare is')
    print('binding clearance to the browser TLS fingerprint, which calibre cannot')
    print('reproduce. Re-harvest first; if a fresh cookie still fails, the')
    print('cookie-bridge approach is not viable on this site.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
