#!/usr/bin/env python3
"""Renew The Economist session cookies by driving a real Chromium.

Why this replaces the QtWebEngine navigator
-------------------------------------------
The previous design drove calibre's own bundled QtWebEngine. That engine is
Chromium **134**, while the profile advertised ``Chrome/151`` (harvested from
the user's actual browser), and calibre's flatpak override passes
``--disable-gpu``, so WebGL did not exist at all. DataDome reads both signals -
UA versus ``Sec-CH-UA`` client hints, and the absence of WebGL - and on
2026-09-03 it reclassified the device and began serving an interstitial
challenge (``x-dd-b``, ``rt:'i'``) to every request. On top of that, QtWebEngine
SIGSEGVs on some machines (Fedora, aarch64), so navigation often died outright.

The machine already had the browser the User-Agent was claiming to be:
``io.github.ungoogled_software.ungoogled_chromium`` 151. Driving *that* over the
Chrome DevTools Protocol means the fingerprint is simply true - real 151, real
WebGL, real client hints - and calibre's crashing engine is out of the renewal
path entirely. Any Chrome/Chromium works; see BROWSER_CMD below.

The second fix is patience. The old navigator snapshotted the DOM 2.5 s after
the first ``loadFinished`` and gave up. A DataDome interstitial needs several
seconds and reloads itself, so that snapshot could only ever catch the
challenge. This polls instead, and only declares failure on timeout.

Modes
-----
``--seed``     inject the cookies from economist_cookies.txt, then navigate
``--refresh``  navigate in the persistent profile, then rewrite the cookie file
``--serve``    --refresh, but leave the browser running and publish its CDP
               endpoint so the recipe can fetch articles through it
``--stop``     shut down a browser left running by --serve
``--status``   report what the profile last did

SECURITY: cookies here are live credentials. The profile is 0700, the cookie
file 0600, and nothing prints a cookie value - only names and lengths.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess  # nosec B404 - argv lists only, never a shell
import sys
import time
import logging
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from economist_session import (
    CHALLENGE_MARKERS,
    CONFIG_DIR,
    COOKIE_DOMAIN,
    COOKIE_FILE,
    INDEX_URL,
    report,
    write_cookie_file,
)

FLATPAK_APP = 'io.github.ungoogled_software.ungoogled_chromium'
# How to start the browser. Override with ECONOMIST_BROWSER_CMD, e.g.
#   ECONOMIST_BROWSER_CMD="google-chrome"   or   "chromium-browser"
#   ECONOMIST_BROWSER_CMD="flatpak run --command=chromium org.chromium.Chromium"
# The default drives the ungoogled-chromium flatpak.
BROWSER_CMD = os.environ.get(
    'ECONOMIST_BROWSER_CMD',
    f'flatpak run --command=chromium {FLATPAK_APP}').split()
# The dedicated profile. Must NOT be the profile you browse with: a second
# Chromium on a profile that is already open exits immediately. Override with
# ECONOMIST_CHROME_PROFILE. The default sits inside the flatpak's own tree so
# the sandboxed browser can write it; a non-flatpak browser wants a plain path.
CHROME_PROFILE = os.path.expanduser(os.environ.get(
    'ECONOMIST_CHROME_PROFILE',
    f'~/.var/app/{FLATPAK_APP}/config/chromium-economist'))
STATUS_FILE = os.path.join(CHROME_PROFILE, 'last-navigation.json')
# Where --serve records the live CDP endpoint for the recipe to connect to.
#
# This deliberately lives in *calibre's* config dir, not the Chromium profile.
# flatpak reserves ~/.var/app: an app granted filesystems=host still sees only
# its own subdirectory there, so anything written under the Chromium app's tree
# is invisible to the recipe running inside calibre's sandbox.
ENDPOINT_FILE = os.path.join(CONFIG_DIR, 'economist-cdp-endpoint.json')
CHALLENGE_DUMP = os.path.join(CHROME_PROFILE, 'last-challenge.html')

# The exact hosts a browser would send to www.economist.com. Never widen this to
# a suffix match: .marber-cdn / p.zephr / g493.economist.com carry their *own*
# __cf_bm, and handing the recipe the CDN's clearance cookie breaks article
# fetches intermittently and invisibly.
COOKIE_HOSTS = ('.economist.com', 'www.economist.com', 'economist.com')

LAUNCH_TIMEOUT_S = 30.0
# A served browser holds a logged-in session behind an unauthenticated (if
# loopback-only) DevTools port. If the recipe crashes before --stop, nothing
# would ever close it, so --serve also starts a detached reaper that shuts the
# browser down after this hard cap. A full edition takes a few minutes.
SERVE_MAX_S = float(os.environ.get('ECONOMIST_SERVE_MAX_S', 45 * 60))
SOLVE_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 1.0
# Give a page that already looks good a moment to finish setting cookies.
SETTLE_S = 2.0

log = logging.getLogger('economist_chrome')
# The best-effort paths above log at debug level. They are silent by default so
# a normal run stays readable; set ECONOMIST_DEBUG=1 to see why something was
# retried, skipped or escalated.
if os.environ.get('ECONOMIST_DEBUG'):
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format='%(name)s %(levelname)s %(message)s')

_DD_RT = re.compile(r"'rt'\s*:\s*'(\w)'")


# --------------------------------------------------------------------------
# Minimal RFC 6455 client
#
# CDP speaks WebSocket and nothing in the stdlib does. Rather than take a
# dependency that has to exist wherever calibre runs this, here is the ~5% of
# the protocol a DevTools client actually needs: a client-masked text channel
# with no extensions and no subprotocol.
# --------------------------------------------------------------------------

class WebSocket:
    def __init__(self, url: str, timeout: float = 90.0) -> None:
        m = re.match(r'ws://([^/:]+):(\d+)(/.*)$', url)
        if not m:
            raise ValueError(f'unsupported websocket url: {url}')
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b''

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f'GET {path} HTTP/1.1\r\n'
            f'Host: {host}:{port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n'
        )
        self.sock.sendall(req.encode())
        header = self._read_until(b'\r\n\r\n')
        if b'101' not in header.split(b'\r\n', 1)[0]:
            raise OSError(f'websocket upgrade refused: {header[:120]!r}')

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError('connection closed during handshake')
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise OSError('connection closed')
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode()
        n = len(payload)
        frame = bytearray([0x81])          # FIN + text
        if n < 126:
            frame.append(0x80 | n)
        elif n < (1 << 16):
            frame.append(0x80 | 126)
            frame += struct.pack('>H', n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack('>Q', n)
        mask = os.urandom(4)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def recv(self) -> str:
        """Next text message, reassembling continuation frames."""
        data = b''
        while True:
            b0, b1 = self._read_exact(2)
            opcode = b0 & 0x0F
            fin = b0 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack('>H', self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('>Q', self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b''

            if opcode == 0x8:
                raise OSError('websocket closed by peer')
            if opcode == 0x9:            # ping -> pong, same payload
                mask = os.urandom(4)
                self.sock.sendall(
                    bytes([0x8A, 0x80 | len(payload)]) + mask
                    + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode == 0xA:            # pong
                continue
            data += payload
            if fin:
                return data.decode('utf-8', 'replace')

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """Chrome DevTools Protocol over one websocket, with flat sessions."""

    def __init__(self, ws_url: str) -> None:
        self.ws = WebSocket(ws_url)
        self._id = 0
        self._pending: dict[int, dict] = {}

    def call(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 60.0) -> dict:
        self._id += 1
        msg_id = self._id
        msg: dict = {'id': msg_id, 'method': method, 'params': params or {}}
        if session_id:
            msg['sessionId'] = session_id
        self.ws.send(json.dumps(msg))

        if msg_id in self._pending:
            return self._unwrap(self._pending.pop(msg_id), method)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == msg_id:
                return self._unwrap(reply, method)
            if 'id' in reply:
                self._pending[reply['id']] = reply
            # Events are not needed: this driver polls rather than subscribes.
        raise TimeoutError(f'CDP {method} timed out')

    @staticmethod
    def _unwrap(reply: dict, method: str) -> dict:
        if 'error' in reply:
            raise RuntimeError(f'CDP {method} failed: {reply["error"]}')
        return reply.get('result', {})

    def close(self) -> None:
        self.ws.close()


# --------------------------------------------------------------------------
# Browser lifecycle
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def launch_browser(port: int) -> subprocess.Popen:
    """Start Chromium on its own profile, off-screen, with CDP enabled.

    Headful on purpose. ``--headless`` is itself a bot signal, and the flatpak
    already holds wayland/x11 sockets. Positioning the window far off-screen
    keeps a scheduled download from stealing focus.
    """
    os.makedirs(CHROME_PROFILE, mode=0o700, exist_ok=True)
    os.chmod(CHROME_PROFILE, 0o700)
    cmd = BROWSER_CMD + [
        f'--user-data-dir={CHROME_PROFILE}',
        f'--remote-debugging-port={port}',
        # No --remote-allow-origins. Chromium admits websocket clients that send
        # no Origin header (this client sends none) and rejects browser-originated
        # ones, which is exactly the split wanted: a web page open elsewhere on
        # this machine must not be able to drive a logged-in session.
        '--no-first-run', '--no-default-browser-check',
        '--disable-features=Translate,MediaRouter',
        # A minimized window is a backgrounded one, and Chromium throttles those
        # hard - timers slowed, renderers suspended. The fetches are driven by
        # explicit CDP calls rather than page timers, but the throttling would
        # still slow them down, so turn it off.
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-background-timer-throttling',
        '--window-position=-32000,-32000',
        '--window-size=1280,1024',
        'about:blank',
    ]
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def wait_for_cdp(port: int, proc: subprocess.Popen) -> str:
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    url = f'http://127.0.0.1:{port}/json/version'
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f'ERROR: chromium exited early (rc={proc.returncode})')
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # nosec B310 - literal http://127.0.0.1
                return json.loads(r.read())['webSocketDebuggerUrl']
        except Exception as e:  # noqa: BLE001 - the port is simply not up yet
            log.debug('CDP not ready on port %s: %r', port, e)
            time.sleep(0.5)
    raise SystemExit(f'ERROR: CDP never came up on port {port}')


def minimize_window(cdp: CDP, target_id: str) -> bool:
    """Get the window off the screen for the rest of the download.

    ``--window-position`` is not enough: this is a Wayland session, and
    compositors there ignore client-requested positions. Asking the window
    manager to minimize, via CDP, works regardless of compositor.

    Deliberately done *after* navigating. The DataDome interstitial has to run
    its JavaScript in a foreground window; minimizing first risks throttling the
    very thing we are waiting for.
    """
    try:
        win = cdp.call('Browser.getWindowForTarget', {'targetId': target_id})
        cdp.call('Browser.setWindowBounds', {
            'windowId': win['windowId'],
            'bounds': {'windowState': 'minimized'},
        })
        return True
    except Exception as e:  # noqa: BLE001 - cosmetic; a visible window is not a failure
        log.debug('could not minimize the window: %r', e)
        return False


def page_endpoint(port: int, target_id: str) -> str:
    """The page target's own websocket URL, from the DevTools HTTP endpoint."""
    with urllib.request.urlopen(  # nosec B310 - literal loopback http URL
            f'http://127.0.0.1:{port}/json/list', timeout=5) as r:
        for t in json.loads(r.read()):
            if t.get('id') == target_id:
                return t['webSocketDebuggerUrl']
    raise RuntimeError('the page target vanished before it could be published')


def write_endpoint(info: dict) -> None:
    fd = os.open(ENDPOINT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(info, f)


def start_reaper(port: int, pid: int) -> None:
    """Detach a watchdog that stops this served browser after SERVE_MAX_S."""
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), '--reap', str(port), str(pid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


REAP_POLL_S = 30.0


def reap(port: int, pid: int) -> int:
    """Stop the served browser once it outlives SERVE_MAX_S.

    Polls rather than sleeping the whole span so that a normal ``--stop`` at the
    end of a download lets this exit within a poll interval, instead of leaving
    an idle process around for the rest of the cap.
    """
    deadline = time.monotonic() + SERVE_MAX_S
    while time.monotonic() < deadline:
        info = read_endpoint()
        if not (info and info.get('port') == port and info.get('pid') == pid):
            return 0            # someone already stopped it; nothing to reap
        time.sleep(min(REAP_POLL_S, max(0.0, deadline - time.monotonic())))
    info = read_endpoint()
    if info and info.get('port') == port and info.get('pid') == pid:
        print(f'Reaping browser served on port {port} after '
              f'{SERVE_MAX_S:.0f}s', flush=True)
        return stop_serving()
    return 0


def read_endpoint() -> dict | None:
    try:
        with open(ENDPOINT_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pid_is_our_browser(pid: int) -> bool:
    """True only if this pid is still the browser we started on our profile.

    Pids are recycled. After a reboot or a crash the recorded pid may belong to
    something else entirely, and signalling it would be someone else's outage.
    """
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmdline = f.read().decode('utf-8', 'replace')
    except OSError:
        return False           # no such process, or not Linux: do not signal
    return CHROME_PROFILE in cmdline


def stop_serving() -> int:
    """Shut down a browser left running by --serve. Safe to call when there isn't one."""
    info = read_endpoint()
    if not info:
        return 0
    cdp = None
    try:
        # The browser websocket URL carries a UUID, so it has to be looked up
        # rather than constructed.
        with urllib.request.urlopen(  # nosec B310 - literal loopback http URL
                f'http://127.0.0.1:{info["port"]}/json/version', timeout=3) as r:
            cdp = CDP(json.loads(r.read())['webSocketDebuggerUrl'])
    except Exception as e:  # noqa: BLE001 - browser already gone; pid fallback below
        log.debug('no reachable DevTools endpoint: %r', e)
        cdp = None
    if cdp is None:
        # No CDP to ask politely; fall back to the recorded pid.
        pid = info.get('pid')
        if pid and pid_is_our_browser(pid):
            try:
                os.kill(pid, 15)
            except OSError as e:
                log.debug('could not signal pid %s: %r', pid, e)
    else:
        try:
            cdp.call('Browser.close', timeout=5)
        except Exception as e:  # noqa: BLE001 - best effort; pid fallback still applies
            log.debug('Browser.close failed: %r', e)
        cdp.close()
    try:
        os.unlink(ENDPOINT_FILE)
    except OSError:
        pass
    return 0


def shutdown(cdp: CDP | None, proc: subprocess.Popen | None) -> None:
    if cdp is not None:
        try:
            cdp.call('Browser.close', timeout=5)
        except Exception as e:  # noqa: BLE001 - teardown must not raise; terminate() follows
            log.debug('Browser.close failed: %r', e)
        cdp.close()
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception as e:  # noqa: BLE001 - refused to exit; escalate to SIGKILL
            log.debug('terminate failed, killing: %r', e)
            proc.kill()


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------

PAGE_PROBE = r"""
(() => {
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const dd = html.match(/'rt'\s*:\s*'(\w)'/);
  return JSON.stringify({
    len: html.length,
    ready: document.readyState,
    next: html.indexOf('__NEXT_DATA__') !== -1,
    markers: %s.filter(m => html.indexOf(m) !== -1),
    rt: dd ? dd[1] : null,
  });
})()
"""


def probe(cdp: CDP, session: str) -> dict:
    js = PAGE_PROBE % json.dumps(list(CHALLENGE_MARKERS))
    res = cdp.call('Runtime.evaluate',
                   {'expression': js, 'returnByValue': True}, session)
    value = res.get('result', {}).get('value')
    if not value:
        return {'len': 0, 'ready': 'unknown', 'next': False, 'markers': [], 'rt': None}
    return json.loads(value)


# Bot-management cookies are deliberately NOT seeded. They identify a *device*,
# and the device we are seeding from is the one DataDome just reclassified -
# importing its cookie would import the block along with it. A fresh profile
# with an honest Chromium 151 fingerprint should be issued its own.
NOT_SEEDED = frozenset({'datadome', '__cf_bm', '_cfuvid', 'cf_clearance'})


def seed_cookies(cdp: CDP, session: str) -> int:
    """Inject the harvested login cookies so a brand-new profile starts signed in."""
    from check_economist_access import load_credentials

    pairs = [(n, v) for n, v in load_credentials(COOKIE_FILE)['cookies']
             if n not in NOT_SEEDED]
    expires = int(time.time()) + 365 * 24 * 3600
    for name, value in pairs:
        cdp.call('Network.setCookie', {
            'name': name, 'value': value,
            'domain': COOKIE_DOMAIN, 'path': '/',
            'secure': True, 'expires': expires,
        }, session)
    return len(pairs)


def collect_cookies(cdp: CDP, session: str) -> list[tuple[str, str]]:
    """Read the live jar over CDP - decrypted, HttpOnly included.

    Chromium 151 encrypts cookie values on disk, and the SQLite jar is only
    committed on a timer, which is what forced the old two-process dance.
    ``Network.getAllCookies`` sidesteps both.
    """
    cookies = cdp.call('Network.getAllCookies', {}, session).get('cookies', [])
    now = time.time()
    latest: dict[str, str] = {}
    for c in cookies:
        if c.get('domain') not in COOKIE_HOSTS:
            continue
        expires = c.get('expires', -1)
        if expires and expires > 0 and expires < now:
            continue
        latest[c['name']] = c.get('value', '')
    return sorted(latest.items())


def write_status(status: dict) -> None:
    fd = os.open(STATUS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(status, f)


def dump_challenge(cdp: CDP, session: str) -> None:
    """Keep the page that beat us. ``{"bytes": 1896}`` explained nothing."""
    try:
        res = cdp.call('Runtime.evaluate', {
            'expression': 'document.documentElement.outerHTML',
            'returnByValue': True}, session)
        html = res.get('result', {}).get('value') or ''
    except Exception as e:  # noqa: BLE001 - diagnostics only; never mask the real failure
        log.debug('could not capture the challenge page: %r', e)
        return
    fd = os.open(CHALLENGE_DUMP, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'Challenge page saved to {CHALLENGE_DUMP}', flush=True)


def run(seed: bool = False, serve: bool = False) -> int:
    """Navigate to renew cookies; with ``serve``, leave the browser running.

    Serve mode exists because calibre's own QtWebEngine worker segfaults on this
    machine, taking the download with it (and hanging, rather than failing). If
    the browser stays up, the recipe can fetch every article and image through
    this same session instead - same cookies, same TLS fingerprint, same origin,
    and no QtWebEngine anywhere in the path.
    """
    if not shutil.which(BROWSER_CMD[0]):
        print(f'ERROR: {BROWSER_CMD[0]} not found (set ECONOMIST_BROWSER_CMD).',
              file=sys.stderr)
        return 1

    stop_serving()          # never leave two browsers on one profile
    port = free_port()
    proc = launch_browser(port)
    cdp = None
    keep = False
    try:
        cdp = CDP(wait_for_cdp(port, proc))
        version = cdp.call('Browser.getVersion')
        user_agent = version['userAgent']
        print(f'Browser: {version["product"]}', flush=True)

        target = cdp.call('Target.createTarget', {'url': 'about:blank'})
        session = cdp.call('Target.attachToTarget',
                           {'targetId': target['targetId'], 'flatten': True}
                           )['sessionId']
        cdp.call('Network.enable', {}, session)
        cdp.call('Page.enable', {}, session)

        if seed:
            print(f'Seeded {seed_cookies(cdp, session)} cookies into '
                  f'{CHROME_PROFILE}', flush=True)

        cdp.call('Page.navigate', {'url': INDEX_URL}, session, timeout=90)

        # Poll rather than snapshot: the DataDome interstitial takes seconds and
        # reloads itself, so any single early sample only ever sees the
        # challenge. Give up on timeout, not on the first unhappy look.
        deadline = time.monotonic() + SOLVE_TIMEOUT_S
        info: dict = {}
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            try:
                info = probe(cdp, session)
            except Exception as e:  # noqa: BLE001 - mid-reload there is no document yet
                log.debug('probe failed, retrying: %r', e)
                continue
            if info['next'] and not info['markers']:
                time.sleep(SETTLE_S)
                break
        else:
            info = info or {'len': 0, 'markers': [], 'rt': None, 'next': False}

        ok = bool(info.get('next')) and not info.get('markers')
        status = {
            'ok': ok,
            'challenged': bool(info.get('markers')),
            'rt': info.get('rt'),
            'bytes': info.get('len', 0),
            'engine': version['product'],
            'at': time.time(),
        }
        write_status(status)

        if not ok:
            dump_challenge(cdp, session)
            rt = info.get('rt')
            kind = {'i': 'interstitial', 'c': 'hard block / captcha'}.get(rt, 'unknown')
            print(f'Navigated: {status["bytes"]} bytes, BLOCKED '
                  f'(DataDome {kind}, rt={rt})', flush=True)
            if rt == 'c':
                print('A hard block means the device is burned: log in again in '
                      'ungoogled-chromium and re-run with --seed.', file=sys.stderr)
            return 1

        cookies = collect_cookies(cdp, session)
        if not cookies:
            print('ERROR: page loaded but the jar held no economist.com cookies.',
                  file=sys.stderr)
            return 1

        print(f'Navigated: {status["bytes"]} bytes, OK', flush=True)
        write_cookie_file(user_agent, cookies)
        print(f'Wrote {COOKIE_FILE} (mode 600)', flush=True)
        report(cookies)

        if serve:
            if minimize_window(cdp, target['targetId']):
                print('Window minimized.', flush=True)
            # Publish the *page* endpoint. A page-level websocket needs no
            # sessionId, which keeps the recipe's client trivial.
            page_ws = page_endpoint(port, target['targetId'])
            write_endpoint({'port': port, 'ws': page_ws, 'pid': proc.pid,
                            'at': time.time()})
            keep = True
            start_reaper(port, proc.pid)
            print(f'Serving CDP on 127.0.0.1:{port}', flush=True)
        return 0
    finally:
        if keep:
            if cdp is not None:
                cdp.close()
        else:
            shutdown(cdp, proc)


def run_status() -> int:
    try:
        with open(STATUS_FILE, encoding='utf-8') as f:
            st = json.load(f)
    except (OSError, ValueError):
        print('No navigation recorded yet.')
        return 0
    print(f'Last navigation: {"OK" if st["ok"] else "BLOCKED"}, '
          f'{(time.time() - st["at"]) / 60:.0f} min ago, '
          f'{st.get("bytes", 0)} bytes, engine {st.get("engine", "?")}'
          + (f', DataDome rt={st["rt"]}' if st.get('rt') else ''))
    return 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else '--refresh'
    if mode == '--refresh':
        return run(seed=False)
    if mode == '--seed':
        return run(seed=True)
    if mode == '--reap' and len(argv) == 4:
        return reap(int(argv[2]), int(argv[3]))
    if mode == '--serve':
        return run(seed=False, serve=True)
    if mode == '--stop':
        return stop_serving()
    if mode == '--status':
        return run_status()
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
