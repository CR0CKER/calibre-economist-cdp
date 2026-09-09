#!/usr/bin/env python3
"""Persistent browser session for The Economist calibre recipe.

Why this exists
---------------
economist.com sits behind a Cloudflare managed challenge. The cookie that gates
access, ``__cf_bm``, lives only 30 minutes - but a real browser never notices,
because Cloudflare issues a fresh one with every response. Copying cookies out of
a browser therefore captures a *snapshot* of a self-renewing value, and it goes
stale almost immediately.

So instead of copying cookies repeatedly, a real, persistent Chromium profile is
kept on disk. It is seeded once from a browser export; after that each refresh
navigates to the site in that profile, which renews the short-lived cookies
automatically. The long-lived login cookies (``fcx_*``) stay in the profile, so
scheduled downloads keep working unattended.

Navigation happens in ``economist_chrome.py``, which drives the user's own
Chromium over the DevTools Protocol. This module owns the cookie-file contract,
the freshness logic and the reporting.

Why not calibre's own engine: it is Chromium 134 while the harvested User-Agent
said Chrome 151, and calibre's flatpak override removes WebGL - DataDome reads
both and reclassifies the device. It also SIGSEGVs on some machines (Fedora,
aarch64). Driving the real browser makes the fingerprint simply true.

Modes
-----
``--seed``       bootstrap from economist_cookies.txt into a fresh Chrome profile
``--refresh``    renew the cookies (delegates to economist_chrome.py)
``--serve``      renew, then leave the browser running and publish its CDP
                 endpoint so the recipe can fetch articles through it
``--stop``       shut down a browser left running by --serve
``--status``     report what is in the cookie file and what last happened

Cookies are read over ``Network.getAllCookies``, which returns them decrypted and
including HttpOnly, so nothing has to touch Chromium's on-disk jar or race its
commit timer.

Usage (on the **host** - it launches the Chromium flatpak)::

    python3 economist_session.py --seed       # once
    python3 economist_session.py --refresh    # renew cookies
    python3 economist_session.py --serve      # renew and stay up (what the recipe does)

SECURITY: the profile and cookie file hold live session credentials. Both are
created 0700/0600, and nothing here ever prints a cookie value - only names and
value lengths.
"""

from __future__ import annotations

import os
import re
import sys
import time

FLATPAK_CONFIG_DIR = os.path.expanduser('~/.var/app/com.calibre_ebook.calibre/config/calibre')


def calibre_config_dir() -> str:
    """calibre's config directory, wherever this calibre keeps it.

    Order: the CALIBRE_CONFIG_DIRECTORY override calibre itself honours, then
    the flatpak location if it exists, then the conventional ~/.config/calibre.
    """
    env = os.environ.get('CALIBRE_CONFIG_DIRECTORY')
    if env:
        return os.path.expanduser(env)
    if os.path.isdir(FLATPAK_CONFIG_DIR):
        return FLATPAK_CONFIG_DIR
    return os.path.expanduser('~/.config/calibre')


CONFIG_DIR = calibre_config_dir()
# Where this script records its own location, so the recipe (which is compiled
# from a string inside calibre and has no __file__ of its own) can find it
# without a hard-coded path. Rewritten on every run; harmless if stale.
SESSION_POINTER = os.path.join(CONFIG_DIR, 'economist-session.path')


def record_location() -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # 0644: the recipe refuses a pointer that is group- or world-writable,
        # since the path in it is imported and executed inside calibre.
        fd = os.open(SESSION_POINTER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(os.path.abspath(__file__) + '\n')
    except OSError:
        pass  # informational only; the env var still works
COOKIE_FILE = os.path.join(CONFIG_DIR, 'economist_cookies.txt')

INDEX_URL = 'https://www.economist.com/weeklyedition'
COOKIE_DOMAIN = '.economist.com'

CHALLENGE_MARKERS = ('Just a moment', 'captcha-delivery', '_cf_chl_opt', 'Please enable JS')

# Cloudflare's __cf_bm lives 30 minutes. Re-navigating while it is comfortably
# fresh buys nothing and just spawns another Chromium, so skip it.
CF_BM_FRESH_MIN = 20.0

_CF_BM_ISSUED = re.compile(r'^[^-]*-(\d{10})\.')


# --------------------------------------------------------------------------
# Cookie file: the contract between this module and the recipe
# --------------------------------------------------------------------------

def write_cookie_file(user_agent: str, cookies: list[tuple[str, str]]) -> None:
    """Rewrite the cookie file the recipe reads, atomically and 0600."""
    header = '; '.join(f'{n}={v}' for n, v in cookies)
    content = (
        '# The Economist browser cookies for the calibre recipe.\n'
        '# CREDENTIALS - keep chmod 600, never commit.\n'
        '# Written by economist_session.py. Do not edit by hand.\n'
        '\n'
        f'USER_AGENT={user_agent}\n'
        f'COOKIE={header}\n'
    )
    tmp = COOKIE_FILE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, COOKIE_FILE)
    os.chmod(COOKIE_FILE, 0o600)


def cf_bm_age_minutes(cookies: list[tuple[str, str]]) -> float | None:
    """Age of __cf_bm in minutes, from the issue time embedded in its value."""
    for name, value in cookies:
        if name == '__cf_bm':
            m = _CF_BM_ISSUED.match(value)
            if m:
                return (time.time() - int(m.group(1))) / 60.0
    return None


def read_cookie_file() -> list[tuple[str, str]]:
    """The cookies as the recipe will see them - backend-independent."""
    if not os.path.exists(COOKIE_FILE):
        return []
    pairs: list[tuple[str, str]] = []
    for line in open(COOKIE_FILE, encoding='utf-8'):
        if not line.startswith('COOKIE='):
            continue
        for chunk in line.split('=', 1)[1].split(';'):
            chunk = chunk.strip()
            if '=' not in chunk:
                continue
            name, _, value = chunk.partition('=')
            if name.strip():
                pairs.append((name.strip(), value.strip()))
    return pairs


def session_is_fresh() -> bool:
    """True when the stored session is recent enough to use without navigating.

    Judged on the cookie file rather than any one backend's jar: that file is
    what the recipe reads, so its __cf_bm is the value whose age actually
    matters.
    """
    age = cf_bm_age_minutes(read_cookie_file())
    return age is not None and age < CF_BM_FRESH_MIN


def report(cookies: list[tuple[str, str]]) -> None:
    """Names and lengths only - never values."""
    print(f'Cookies in profile: {len(cookies)}', flush=True)
    names = {n for n, _ in cookies}
    for wanted in ('__cf_bm', 'datadome', 'fcx_user', 'fcx_access_token',
                   'state-is-subscriber'):
        print(f'  {wanted}: {"present" if wanted in names else "MISSING"}', flush=True)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def run_refresh(seed: bool = False) -> int:
    """Renew the cookies by driving the real browser, unless they are still fresh."""
    if not seed and session_is_fresh():
        print('Session still fresh; skipping navigation.', flush=True)
        report(read_cookie_file())
        return 0

    import economist_chrome
    return economist_chrome.run(seed=seed)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else '--refresh'
    record_location()
    if mode == '--refresh':
        return run_refresh(seed=False)
    if mode == '--seed':
        return run_refresh(seed=True)
    if mode == '--serve':
        # Refresh, then leave the browser up so the recipe can fetch through it.
        import economist_chrome
        return economist_chrome.run(seed=False, serve=True)
    if mode == '--stop':
        import economist_chrome
        return economist_chrome.stop_serving()
    if mode == '--status':
        cookies = read_cookie_file()
        report(cookies)
        age = cf_bm_age_minutes(cookies)
        if age is not None:
            print(f'  __cf_bm age: {age:.0f} min '
                  f'({"fresh" if age < CF_BM_FRESH_MIN else "stale"})')
        import economist_chrome
        return economist_chrome.run_status()
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
