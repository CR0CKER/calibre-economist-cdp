#!/usr/bin/env python3
"""Preflight check: does the served browser session reach economist.com?

economist.com sits behind a Cloudflare managed JS challenge plus DataDome, so the
only transport that works is the user's own browser, driven over CDP. This script
answers "will a download work right now?" in seconds instead of after a
fifteen-minute failure: it reports the cookie file's state and then fetches the
index through the session ``economist_session.py --serve`` left running.

Run it on the host, after --serve::

    python3 economist_session.py --serve
    python3 check_economist_access.py

Exit codes: 0 = the session reaches the page, 1 = it does not,
2 = cookie file missing or malformed.

SECURITY: cookie values are credentials. This script prints cookie *names* and
value *lengths* only, never the values themselves.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
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


def probe_cdp(creds: Credentials) -> ProbeResult:
    """Fetch the index through the served Chromium session - the real transport.

    Requires ``economist_session.py --serve`` to have left a browser running;
    that is what the recipe uses, so this answers the question the recipe cares
    about rather than testing a transport nothing uses any more.

    Classification happens *in the page*: an edition index is megabytes, and
    shipping it back over the websocket only to search it here would be slow and
    would risk truncating away the very marker being looked for.
    """
    from economist_chrome import CDP, read_endpoint

    info = read_endpoint()
    if info is None:
        return {'transport': 'cdp', 'ok': False,
                'detail': 'no served browser (run economist_session.py --serve)'}
    markers = [m.decode() for m in CHALLENGE_MARKERS]
    expr = """
    (async () => {
      const r = await fetch(%s, {credentials: 'include'});
      const t = await r.text();
      return JSON.stringify({
        status: r.status, len: t.length,
        marker: %s.find(m => t.includes(m)) || null,
        next: t.includes(%s),
      });
    })()
    """ % (json.dumps(INDEX_URL), json.dumps(markers),
           json.dumps(SUCCESS_MARKER.decode()))

    cdp = CDP(info['ws'])
    try:
        reply = cdp.call('Runtime.evaluate', {
            'expression': expr, 'awaitPromise': True, 'returnByValue': True,
        }, timeout=120)
    finally:
        cdp.close()
    page = json.loads(reply['result']['value'])

    if page['marker']:
        detail = (f'blocked - challenge page ({page["marker"]}), '
                  f'HTTP {page["status"]}, {page["len"]} chars')
        return {'transport': 'cdp', 'ok': False, 'detail': detail}
    if page['next']:
        detail = (f'OK - __NEXT_DATA__ present, HTTP {page["status"]}, '
                  f'{page["len"]} chars')
        return {'transport': 'cdp', 'ok': True, 'detail': detail}
    return {'transport': 'cdp', 'ok': False,
            'detail': (f'unexpected page - no __NEXT_DATA__ and no challenge '
                       f'marker, HTTP {page["status"]}, {page["len"]} chars')}


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
        verdict = f'{remaining:.0f} min left' if remaining > 0 else 'EXPIRED - refresh'
        print(f'Cookie age  : {age:.1f} min of {CF_BM_LIFETIME_MIN:.0f} ({verdict})')
    present = {n for n, _ in creds['cookies']}
    for wanted in sorted({'cf_clearance', 'datadome'}):
        print(f'    -> {wanted}: {"present" if wanted in present else "MISSING"}')
    print()

    try:
        result = probe_cdp(creds)
    except Exception as e:  # noqa: BLE001 - report any failure, never traceback at the user
        result = {'transport': 'cdp', 'ok': False, 'detail': f'{type(e).__name__}: {e}'}
    print(f'[{"PASS" if result["ok"] else "FAIL"}] cdp        {result["detail"]}', flush=True)
    print()

    if result['ok']:
        print('GO: the served browser session reaches the real page.')
        return 0
    print('NO-GO: the served session did not reach the page.')
    print('Run economist_session.py --refresh and read what it reports; a')
    print('DataDome rt=c means the device is blocked and needs a re-seed.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
