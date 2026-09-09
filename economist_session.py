#!/usr/bin/env python3
"""Persistent browser session for The Economist calibre recipe.

Why this exists
---------------
economist.com sits behind a Cloudflare managed challenge. The cookie that gates
access, ``__cf_bm``, lives only 30 minutes - but a real browser never notices,
because Cloudflare issues a fresh one with every response. Copying cookies out of
a browser therefore captures a *snapshot* of a self-renewing value, and it goes
stale almost immediately.

So instead of copying cookies repeatedly, this keeps a real, persistent
QtWebEngine profile on disk. It is seeded once from a browser export; after that
each refresh navigates to the site, which renews the short-lived cookies
automatically. The long-lived login cookies (``fcx_*``) stay in the profile, so
scheduled downloads keep working unattended.

Unlike calibre's scraper - whose backend never navigates, and so cannot execute a
page's JavaScript - this loads pages properly, which is what lets the challenge
resolve.

2026-09-03: the navigation engine moved out of calibre's QtWebEngine
------------------------------------------------------------------
Driving calibre's own engine meant Chromium **134** wearing a ``Chrome/151``
User-Agent, with ``--disable-gpu`` removing WebGL entirely. DataDome reads both
and reclassified the device, serving an interstitial challenge to every request.
That engine also SIGSEGVs on some machines (Fedora, aarch64).

Navigation therefore happens in ``economist_chrome.py``, which drives the real
ungoogled-chromium 151 the User-Agent was always claiming to be. This module
keeps the cookie-file contract, the freshness logic and the reporting; the Qt
navigator survives as ``--navigate-qt`` in case that browser is ever absent.

Modes
-----
``--seed``       bootstrap from economist_cookies.txt into a fresh Chrome profile
``--refresh``    renew the cookies (delegates to economist_chrome.py)
``--serve``      renew, then leave the browser running and publish its CDP
                 endpoint so the recipe can fetch articles through it
``--stop``       shut down a browser left running by --serve
``--status``     report what is in the cookie file and what last happened
``--export``     legacy: read the Qt profile DB, write the cookie file
``--navigate-qt``legacy: the old QtWebEngine navigator

The two phases are separate processes on purpose. Chromium commits its cookie
jar to disk as the process tears down, so reading the database from inside the Qt
process races that commit and silently loses freshly-set cookies. Splitting them
makes the flush a precondition rather than a hope, and it also means a segfault
during Qt teardown - which happens on this box - cannot cost us the cookies.

Usage (on the **host** - it launches the Chromium flatpak)::

    python3 economist_session.py --seed       # once
    python3 economist_session.py --refresh    # renew cookies
    python3 economist_session.py --serve      # renew and stay up (what the recipe does)

SECURITY: the profile and cookie file hold live session credentials. Both are
created 0700/0600, and nothing here ever prints a cookie value - only names and
value lengths.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import TypedDict

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
        with open(SESSION_POINTER, 'w', encoding='utf-8') as f:
            f.write(os.path.abspath(__file__) + '\n')
    except OSError:
        pass  # informational only; the env var still works
COOKIE_FILE = os.path.join(CONFIG_DIR, 'economist_cookies.txt')
PROFILE_DIR = os.path.join(CONFIG_DIR, 'economist-profile')
STATUS_FILE = os.path.join(PROFILE_DIR, 'last-navigation.json')

INDEX_URL = 'https://www.economist.com/weeklyedition'
COOKIE_DOMAIN = '.economist.com'
LOAD_TIMEOUT_MS = 90_000
SETTLE_MS = 2500

CHALLENGE_MARKERS = ('Just a moment', 'captcha-delivery', '_cf_chl_opt', 'Please enable JS')

# Chromium stores timestamps as microseconds since 1601-01-01.
CHROMIUM_EPOCH_OFFSET = 11_644_473_600

# A navigation older than this is not trustworthy evidence that the session works.
STATUS_MAX_AGE_S = 15 * 60

# Cloudflare's __cf_bm lives 30 minutes. Re-navigating while it is comfortably
# fresh buys nothing and just spawns another Chromium, so skip it.
CF_BM_FRESH_MIN = 20.0

_CF_BM_ISSUED = re.compile(r'^[^-]*-(\d{10})\.')


class NavStatus(TypedDict):
    """Result of the Qt navigation phase, handed to the export phase on disk."""

    ok: bool
    challenged: bool
    bytes: int
    at: float


# --------------------------------------------------------------------------
# Shared helpers (no Qt)
# --------------------------------------------------------------------------

def read_user_agent() -> str:
    """The User-Agent harvested with the cookies. It must stay stable."""
    if not os.path.exists(COOKIE_FILE):
        raise SystemExit(
            f'ERROR: {COOKIE_FILE} not found. Import a browser export first:\n'
            '  python3 import_curl_cookies.py <curl.txt>'
        )
    for line in open(COOKIE_FILE, encoding='utf-8'):
        if line.startswith('USER_AGENT='):
            ua = line.split('=', 1)[1].strip()
            if ua and not ua.startswith('<'):
                return ua
    raise SystemExit(f'ERROR: no usable USER_AGENT in {COOKIE_FILE}')


def read_profile_cookies() -> list[tuple[str, str]]:
    """Read the live cookie jar straight from the profile's own database.

    The obvious approach - QWebEngineCookieStore.loadAllCookies() - silently omits
    HttpOnly cookies, which drops __cf_bm: precisely the one that matters. The
    profile's SQLite store has them all, and QtWebEngine keeps values in
    plaintext, so read it directly. Expired rows are skipped; newest wins.
    """
    import sqlite3

    db = os.path.join(PROFILE_DIR, 'Cookies')
    if not os.path.exists(db):
        return []

    now = time.time()
    con = sqlite3.connect(f'file:{db}?immutable=1', uri=True)
    try:
        # Exactly the hosts a browser would send to www.economist.com: the domain
        # cookie and the host-only cookie. A LIKE '%economist.com' match is WRONG -
        # it also pulls in .marber-cdn.economist.com, p.zephr.economist.com and
        # friends, which carry their *own* __cf_bm. Deduping by name then picks
        # whichever was written last, handing the recipe the CDN's clearance
        # cookie and breaking article fetches intermittently.
        rows = con.execute(
            'SELECT name, value, expires_utc, has_expires FROM cookies '
            'WHERE host_key IN (?, ?) ORDER BY last_update_utc ASC',
            (COOKIE_DOMAIN, 'www.economist.com'),
        ).fetchall()
    finally:
        con.close()

    latest: dict[str, str] = {}
    for name, value, expires_utc, has_expires in rows:
        if has_expires and expires_utc:
            if expires_utc / 1_000_000 - CHROMIUM_EPOCH_OFFSET < now:
                continue
        latest[name] = value
    return sorted(latest.items())


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


def read_status() -> NavStatus | None:
    try:
        with open(STATUS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
# Qt phase
# --------------------------------------------------------------------------

def newest_cookie_update() -> float:
    """Unix time of the most recent cookie write, or 0.0 if unreadable.

    Used to confirm Chromium has actually committed the jar to disk. A read-only
    (not immutable) connection is opened fresh each call, because the file is
    being written by this very process.
    """
    import sqlite3

    db = os.path.join(PROFILE_DIR, 'Cookies')
    if not os.path.exists(db):
        return 0.0
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            row = con.execute(
                'SELECT MAX(last_update_utc) FROM cookies WHERE host_key IN (?, ?)',
                (COOKIE_DOMAIN, 'www.economist.com'),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return 0.0
    if not row or not row[0]:
        return 0.0
    return row[0] / 1_000_000 - CHROMIUM_EPOCH_OFFSET


def wait_for_cookie_commit(app, started_at: float, timeout_s: float = 20.0) -> bool:
    """Pump the event loop until Chromium writes the jar, or we give up.

    Chromium commits cookies on a timer and at teardown. Waiting for the commit
    means we can exit hard afterwards - avoiding Qt's teardown, which segfaults
    on this machine and surfaces as a crash dialog in the calibre GUI.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if newest_cookie_update() >= started_at:
            return True
        time.sleep(0.25)
    return False


def run_navigate() -> int:  # legacy - see the module docstring
    """Load the site in the persistent profile so its cookies renew."""
    # QtWebEngine must be imported before QApplication is constructed - it sets
    # Qt.AA_ShareOpenGLContexts, and without that creating a profile segfaults.
    from qt.core import (QApplication, QByteArray, QDateTime, QNetworkCookie,
                         QTimer, QUrl)
    from qt.webengine import QWebEnginePage, QWebEngineProfile

    seed = os.environ.get('ECONOMIST_SEED') == '1'
    user_agent = read_user_agent()

    os.makedirs(PROFILE_DIR, mode=0o700, exist_ok=True)
    os.chmod(PROFILE_DIR, 0o700)

    app = QApplication(sys.argv[:1])
    profile = QWebEngineProfile('economist', app)
    profile.setPersistentStoragePath(PROFILE_DIR)
    profile.setCachePath(PROFILE_DIR)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpUserAgent(user_agent)

    if seed:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from check_economist_access import load_credentials

        # A cookie with no expiry is a *session* cookie, which Chromium never
        # writes to disk - seeding without one leaves an empty profile.
        expiry = QDateTime.currentDateTime().addYears(1)
        store = profile.cookieStore()
        seeded = load_credentials(COOKIE_FILE)['cookies']
        for name, value in seeded:
            c = QNetworkCookie(QByteArray(name.encode()), QByteArray(value.encode()))
            c.setDomain(COOKIE_DOMAIN)
            c.setPath('/')
            c.setExpirationDate(expiry)
            store.setCookie(c, QUrl('https://www.economist.com/'))
        print(f'Seeded {len(seeded)} cookies into {PROFILE_DIR}', flush=True)

    page = QWebEnginePage(profile, app)
    result: dict[str, str] = {}

    def on_load_finished(_ok: bool) -> None:
        def on_html(html: str) -> None:
            result['html'] = html
            QTimer.singleShot(SETTLE_MS, app.quit)

        page.toHtml(on_html)

    started_at = time.time()
    page.loadFinished.connect(on_load_finished)
    page.load(QUrl(INDEX_URL))
    QTimer.singleShot(LOAD_TIMEOUT_MS, app.quit)
    app.exec()

    html = result.get('html', '')
    challenged = any(m in html for m in CHALLENGE_MARKERS)
    status: NavStatus = {
        'ok': '__NEXT_DATA__' in html and not challenged,
        'challenged': challenged,
        'bytes': len(html),
        'at': time.time(),
    }
    # Write the verdict before teardown: Qt sometimes segfaults on exit here, and
    # the export phase must still learn whether the navigation worked.
    fd = os.open(STATUS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(status, f)

    committed = wait_for_cookie_commit(app, started_at)
    print(f'Navigated: {status["bytes"]} bytes, '
          f'{"OK" if status["ok"] else "BLOCKED"}'
          f'{" (challenge)" if challenged else ""}'
          f'{"" if committed else " [cookie commit not confirmed]"}', flush=True)
    sys.stdout.flush()
    sys.stderr.flush()

    # Exit before Qt tears itself down. Its teardown segfaults on this machine
    # (SIGSEGV inside libQt6WebEngineCore), which the calibre GUI reports to the
    # user as a crash. Everything we need is already on disk: the status file was
    # written above and the cookie commit was just confirmed.
    os._exit(0 if status['ok'] else 1)


# --------------------------------------------------------------------------
# Export phase (no Qt)
# --------------------------------------------------------------------------

def run_export() -> int:
    status = read_status()
    if status is None:
        print('ERROR: no navigation status found; run --navigate first.',
              file=sys.stderr)
        return 1
    age = time.time() - status['at']
    if age > STATUS_MAX_AGE_S:
        print(f'ERROR: last navigation was {age / 60:.0f} min ago; run --refresh.',
              file=sys.stderr)
        return 1

    cookies = read_profile_cookies()
    if not status['ok']:
        print('ERROR: the last navigation was blocked. Re-import a fresh browser '
              'export and run --seed again.', file=sys.stderr)
        report(cookies)
        return 1
    if not cookies:
        print('ERROR: the profile holds no cookies.', file=sys.stderr)
        return 1

    write_cookie_file(read_user_agent(), cookies)
    print(f'Wrote {COOKIE_FILE} (mode 600)', flush=True)
    report(cookies)
    return 0


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
    if mode == '--navigate-qt':       # legacy QtWebEngine navigator
        return run_navigate()
    if mode == '--export':            # legacy: export from the Qt profile
        return run_export()
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
