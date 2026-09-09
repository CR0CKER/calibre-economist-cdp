"""Tests for the Chromium CDP session driver.

All cookie values here are synthetic; no real credentials appear.

The parts worth testing are the ones that failed silently in the previous
design: which cookies get picked out of the jar, which ones must *not* be
carried into a fresh profile, and the hand-rolled WebSocket framing that has no
library behind it.
"""

from __future__ import annotations

import json
import os
import struct
import time

import pytest

import economist_chrome as mod
import economist_session as session

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/151.0.0.0 Safari/537.36')


# --------------------------------------------------------------------------
# Cookie selection
# --------------------------------------------------------------------------

def cookie(name: str, value: str = 'v', domain: str = '.economist.com',
           expires: float = -1) -> dict:
    return {'name': name, 'value': value, 'domain': domain, 'expires': expires}


class FakeCDP:
    """Stands in for a CDP connection, recording the calls made against it."""

    def __init__(self, cookies: list[dict] | None = None) -> None:
        self.cookies = cookies or []
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None, session_id=None, timeout=60.0):
        self.calls.append((method, params or {}))
        if method == 'Network.getAllCookies':
            return {'cookies': self.cookies}
        return {}


def test_collects_only_the_two_hosts_a_browser_would_send() -> None:
    cdp = FakeCDP([
        cookie('__cf_bm', 'real', '.economist.com'),
        cookie('__cf_bm', 'cdn', '.marber-cdn.economist.com'),
        cookie('__cf_bm', 'zephr', 'p.zephr.economist.com'),
        cookie('AWSALB', 'edge', 'g493.economist.com'),
        cookie('fcx_user', 'me', 'www.economist.com'),
    ])
    got = dict(mod.collect_cookies(cdp, 's'))
    assert got['__cf_bm'] == 'real'
    assert got['fcx_user'] == 'me'
    assert 'AWSALB' not in got


def test_cdn_cookie_never_shadows_the_real_clearance_cookie() -> None:
    """The 47-of-72 IndexError bug: a suffix match hands over the CDN's __cf_bm."""
    cdp = FakeCDP([
        cookie('__cf_bm', 'cdn', '.marber-cdn.economist.com'),
        cookie('__cf_bm', 'real', '.economist.com'),
    ])
    assert dict(mod.collect_cookies(cdp, 's'))['__cf_bm'] == 'real'


def test_expired_cookies_are_dropped() -> None:
    cdp = FakeCDP([
        cookie('stale', 'x', expires=time.time() - 60),
        cookie('live', 'y', expires=time.time() + 600),
        cookie('sessiononly', 'z', expires=-1),
    ])
    got = dict(mod.collect_cookies(cdp, 's'))
    assert 'stale' not in got
    assert got['live'] == 'y'
    assert got['sessiononly'] == 'z'


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------

def test_bot_management_cookies_are_not_seeded(monkeypatch, tmp_path) -> None:
    """Seeding the burned device cookie would import the block along with it."""
    creds = {'user_agent': UA, 'cookies': [
        ('fcx_user', 'keep'), ('datadome', 'burned'),
        ('__cf_bm', 'burned'), ('_cfuvid', 'burned'),
        ('cf_clearance', 'burned'), ('state-is-subscriber', 'keep'),
    ]}
    import check_economist_access
    monkeypatch.setattr(check_economist_access, 'load_credentials',
                        lambda path=None: creds)

    cdp = FakeCDP()
    assert mod.seed_cookies(cdp, 's') == 2
    seeded = {p['name'] for m, p in cdp.calls if m == 'Network.setCookie'}
    assert seeded == {'fcx_user', 'state-is-subscriber'}


def test_seeded_cookies_carry_an_explicit_expiry(monkeypatch) -> None:
    """Without one they are session cookies and never reach disk."""
    import check_economist_access
    monkeypatch.setattr(check_economist_access, 'load_credentials',
                        lambda path=None: {'user_agent': UA,
                                           'cookies': [('fcx_user', 'x')]})
    cdp = FakeCDP()
    mod.seed_cookies(cdp, 's')
    params = [p for m, p in cdp.calls if m == 'Network.setCookie'][0]
    assert params['expires'] > time.time()
    assert params['secure'] is True
    assert params['domain'] == mod.COOKIE_DOMAIN


# --------------------------------------------------------------------------
# Challenge detection
# --------------------------------------------------------------------------

def test_probe_reports_challenge_markers_and_datadome_type() -> None:
    html = ("<html><body><script>var dd={'rt':'i','cid':'x'}</script>"
            "<p>Please enable JS</p></body></html>")

    class ProbeCDP(FakeCDP):
        def call(self, method, params=None, session_id=None, timeout=60.0):
            assert method == 'Runtime.evaluate'
            # Evaluate the probe the way the browser would, over this html.
            markers = [m for m in mod.CHALLENGE_MARKERS if m in html]
            import re
            rt = re.search(r"'rt'\s*:\s*'(\w)'", html)
            return {'result': {'value': json.dumps({
                'len': len(html), 'ready': 'complete',
                'next': '__NEXT_DATA__' in html,
                'markers': markers, 'rt': rt.group(1) if rt else None})}}

    info = mod.probe(ProbeCDP(), 's')
    assert info['rt'] == 'i'
    assert info['markers']
    assert not info['next']


def test_probe_survives_an_evaluation_that_returns_nothing() -> None:
    class EmptyCDP(FakeCDP):
        def call(self, method, params=None, session_id=None, timeout=60.0):
            return {'result': {}}

    info = mod.probe(EmptyCDP(), 's')
    assert info['next'] is False
    assert info['markers'] == []


def test_probe_expression_embeds_the_shared_challenge_markers() -> None:
    js = mod.PAGE_PROBE % json.dumps(list(mod.CHALLENGE_MARKERS))
    for marker in mod.CHALLENGE_MARKERS:
        assert marker in js


# --------------------------------------------------------------------------
# WebSocket framing
# --------------------------------------------------------------------------

class FakeSocket:
    """A socket whose sent frames can be inspected and replayed back."""

    def __init__(self, inbound: bytes = b'') -> None:
        self.sent = b''
        self.inbound = inbound

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if not self.inbound:
            raise OSError('closed')
        out, self.inbound = self.inbound[:n], self.inbound[n:]
        return out

    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        pass


def make_ws(inbound: bytes = b'') -> mod.WebSocket:
    ws = mod.WebSocket.__new__(mod.WebSocket)
    ws.sock = FakeSocket(inbound)
    ws._buf = b''
    return ws


def unmask(frame: bytes) -> bytes:
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack('>H', frame[2:4])[0]
        offset = 4
    elif length == 127:
        length = struct.unpack('>Q', frame[2:10])[0]
        offset = 10
    mask = frame[offset:offset + 4]
    body = frame[offset + 4:offset + 4 + length]
    return bytes(b ^ mask[i % 4] for i, b in enumerate(body))


def server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """A server->client frame: same layout, but unmasked."""
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    if n < (1 << 16):
        return head + bytes([126]) + struct.pack('>H', n) + payload
    return head + bytes([127]) + struct.pack('>Q', n) + payload


@pytest.mark.parametrize('size', [5, 200, 70000])
def test_sent_frames_are_masked_and_round_trip(size: int) -> None:
    text = 'x' * size
    ws = make_ws()
    ws.send(text)
    assert ws.sock.sent[0] == 0x81           # FIN + text
    assert ws.sock.sent[1] & 0x80            # client frames must be masked
    assert unmask(ws.sock.sent).decode() == text


def test_receives_a_message_split_across_continuation_frames() -> None:
    inbound = (server_frame(b'{"id":', opcode=0x1, fin=False)
               + server_frame(b'1}', opcode=0x0, fin=True))
    assert make_ws(inbound).recv() == '{"id":1}'


def test_ping_is_answered_and_does_not_surface_as_a_message() -> None:
    inbound = server_frame(b'hi', opcode=0x9) + server_frame(b'"pong-later"')
    ws = make_ws(inbound)
    assert ws.recv() == '"pong-later"'
    assert ws.sock.sent[0] == 0x8A           # a pong went back
    assert unmask(ws.sock.sent) == b'hi'


def test_close_frame_raises_rather_than_hanging() -> None:
    with pytest.raises(OSError):
        make_ws(server_frame(b'', opcode=0x8)).recv()


# --------------------------------------------------------------------------
# CDP request/reply correlation
# --------------------------------------------------------------------------

class ScriptedWS:
    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv(self) -> str:
        return json.dumps(self.replies.pop(0))

    def close(self) -> None:
        pass


def make_cdp(replies: list[dict]) -> mod.CDP:
    cdp = mod.CDP.__new__(mod.CDP)
    cdp.ws = ScriptedWS(replies)
    cdp._id = 0
    cdp._pending = {}
    return cdp


def test_out_of_order_replies_are_matched_to_their_request() -> None:
    cdp = make_cdp([
        {'method': 'Page.loadEventFired', 'params': {}},   # an event, ignored
        {'id': 99, 'result': {'stale': True}},             # someone else's id
        {'id': 1, 'result': {'ok': True}},
    ])
    assert cdp.call('Page.enable') == {'ok': True}


def test_a_protocol_error_is_raised_not_returned() -> None:
    cdp = make_cdp([{'id': 1, 'error': {'message': 'no such target'}}])
    with pytest.raises(RuntimeError, match='no such target'):
        cdp.call('Target.attachToTarget')


def test_session_id_is_attached_when_given() -> None:
    cdp = make_cdp([{'id': 1, 'result': {}}])
    cdp.call('Network.enable', session_id='abc')
    assert cdp.ws.sent[0]['sessionId'] == 'abc'


# --------------------------------------------------------------------------
# Cookie file round trip (the artefact the recipe actually reads)
# --------------------------------------------------------------------------

def test_cookie_file_round_trips_values_containing_equals(tmp_path, monkeypatch) -> None:
    """JWTs and base64 padding carry '=' - splitting on every '=' corrupts them."""
    path = tmp_path / 'economist_cookies.txt'
    monkeypatch.setattr(session, 'COOKIE_FILE', str(path))
    pairs = [('__cf_bm', 'a-1788462661.18-1.0.1.1-Yhu=='), ('fcx_user', 'e30=')]
    session.write_cookie_file(UA, pairs)
    assert session.read_cookie_file() == pairs


def test_cookie_file_is_written_private(tmp_path, monkeypatch) -> None:
    import os
    import stat
    path = tmp_path / 'economist_cookies.txt'
    monkeypatch.setattr(session, 'COOKIE_FILE', str(path))
    session.write_cookie_file(UA, [('a', 'b')])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_freshness_is_judged_from_the_cookie_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / 'economist_cookies.txt'
    monkeypatch.setattr(session, 'COOKIE_FILE', str(path))

    fresh = int(time.time())
    session.write_cookie_file(UA, [('__cf_bm', f'tok-{fresh}.1-1.0.1.1-abc')])
    assert session.session_is_fresh() is True

    old = int(time.time()) - 40 * 60
    session.write_cookie_file(UA, [('__cf_bm', f'tok-{old}.1-1.0.1.1-abc')])
    assert session.session_is_fresh() is False


def test_missing_cookie_file_is_not_fresh(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session, 'COOKIE_FILE', str(tmp_path / 'absent.txt'))
    assert session.read_cookie_file() == []
    assert session.session_is_fresh() is False


# --------------------------------------------------------------------------
# Serve mode: the endpoint handshake between host and sandbox
# --------------------------------------------------------------------------

def test_endpoint_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(tmp_path / 'ep.json'))
    mod.write_endpoint({'port': 1234, 'ws': 'ws://127.0.0.1:1234/devtools/page/A',
                        'pid': 99, 'at': time.time()})
    assert mod.read_endpoint()['port'] == 1234


def test_endpoint_is_written_private(tmp_path, monkeypatch) -> None:
    import os
    import stat
    path = tmp_path / 'ep.json'
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(path))
    mod.write_endpoint({'port': 1})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_missing_endpoint_reads_as_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(tmp_path / 'absent.json'))
    assert mod.read_endpoint() is None


def test_corrupt_endpoint_reads_as_none(tmp_path, monkeypatch) -> None:
    path = tmp_path / 'ep.json'
    path.write_text('{not json')
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(path))
    assert mod.read_endpoint() is None


def test_stop_serving_is_a_noop_with_no_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(tmp_path / 'absent.json'))
    assert mod.stop_serving() == 0


def test_stop_serving_removes_the_endpoint_and_kills_the_pid(
        tmp_path, monkeypatch) -> None:
    """With no CDP reachable it must still not leave a stale endpoint behind."""
    path = tmp_path / 'ep.json'
    monkeypatch.setattr(mod, 'ENDPOINT_FILE', str(path))
    mod.write_endpoint({'port': 1, 'pid': 4242})

    killed = []
    monkeypatch.setattr(mod.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    assert mod.stop_serving() == 0
    assert killed == [(4242, 15)]
    assert not path.exists()


def test_endpoint_lives_outside_the_chromium_app_dir() -> None:
    """flatpak reserves ~/.var/app: calibre sees only its own subtree there.

    An endpoint written under the Chromium app's directory is invisible to the
    recipe, which is exactly how this failed the first time.
    """
    assert 'ungoogled' not in mod.ENDPOINT_FILE
    assert 'com.calibre_ebook.calibre' in mod.ENDPOINT_FILE


# --- config-dir discovery and the session pointer file ----------------------

def test_calibre_config_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv('CALIBRE_CONFIG_DIRECTORY', str(tmp_path))
    assert session.calibre_config_dir() == str(tmp_path)


def test_calibre_config_dir_falls_back_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv('CALIBRE_CONFIG_DIRECTORY', raising=False)
    monkeypatch.setattr(session, 'FLATPAK_CONFIG_DIR', str(tmp_path / 'absent'))
    monkeypatch.setenv('HOME', str(tmp_path))
    assert session.calibre_config_dir() == str(tmp_path / '.config' / 'calibre')


def test_record_location_writes_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr(session, 'CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(session, 'SESSION_POINTER', str(tmp_path / 'economist-session.path'))
    session.record_location()
    recorded = (tmp_path / 'economist-session.path').read_text().strip()
    assert recorded == os.path.abspath(session.__file__)
