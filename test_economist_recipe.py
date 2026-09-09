"""Tests for the recipe itself: the CDP browser shim, the fetch contract, the
session-script lookup, and the JSON-to-HTML article parser.

The recipe is loaded by `conftest.py` with calibre's imports stubbed. See that
file for what is stubbed and for the one gap this suite does not cover (index
parsing, which needs a working html5_parser/lxml pair).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error

import pytest


# --------------------------------------------------------------------------
# CDPBrowser: the surface calibre's fetcher calls
# --------------------------------------------------------------------------

class FakeCDP:
    """Stands in for the websocket. Records calls, replays queued page results."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.closed = False

    def call(self, method, params, timeout=None):
        self.calls.append((method, params, timeout))
        if not self.results:
            return {'result': {'value': None}}
        return {'result': {'value': json.dumps(self.results.pop(0))}}

    def close(self):
        self.closed = True


def make_browser(recipe, monkeypatch, results=None):
    """A CDPBrowser wired to a FakeCDP instead of a real websocket."""
    fake = FakeCDP(results)
    monkeypatch.setattr(recipe.CDPBrowser, '__init__',
                        lambda self, ws, ua, log=None: None)
    br = recipe.CDPBrowser('ws://unused', 'UA/1.0')
    import threading
    br._cdp = fake
    br._lock = threading.Lock()
    br._ua = 'UA/1.0'
    br._log = None
    return br, fake


def page_result(body: bytes, status: int = 200, ctype: str = 'text/html',
                url: str = 'https://www.economist.com/x') -> dict:
    return {'ok': True, 'status': status, 'url': url, 'type': ctype,
            'b64': base64.b64encode(body).decode()}


def test_fetches_page_bytes_through_the_session(recipe, monkeypatch):
    br, fake = make_browser(recipe, monkeypatch, [page_result(b'<html>hi</html>')])
    resp = br.open_novisit('https://www.economist.com/weeklyedition')
    assert resp.read() == b'<html>hi</html>'
    assert resp.getcode() == 200
    method, params, _ = fake.calls[0]
    assert method == 'Runtime.evaluate'
    assert params['awaitPromise'] is True
    # The URL must reach the page as a JSON literal, never as raw interpolation.
    assert json.dumps('https://www.economist.com/weeklyedition') in params['expression']


def test_url_with_a_quote_cannot_break_out_of_the_in_page_script(recipe, monkeypatch):
    br, fake = make_browser(recipe, monkeypatch, [page_result(b'ok')])
    nasty = "https://www.economist.com/a'; fetch('https://evil.example'); //"
    br.open_novisit(nasty)
    expression = fake.calls[0][1]['expression']
    assert json.dumps(nasty) in expression
    # the payload survives only as an escaped string literal
    assert "'; fetch(" not in expression.replace(json.dumps(nasty), '')


def test_binary_body_survives_the_base64_round_trip(recipe, monkeypatch):
    png = bytes(range(256)) * 8
    br, _ = make_browser(recipe, monkeypatch, [page_result(png, ctype='image/png')])
    assert br.open_novisit('https://www.economist.com/i.png').read() == png


def test_http_error_status_raises_httperror(recipe, monkeypatch):
    br, _ = make_browser(recipe, monkeypatch, [page_result(b'nope', status=403)])
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        br.open_novisit('https://www.economist.com/x')
    assert excinfo.value.code == 403


def test_failed_in_page_fetch_raises_urlerror(recipe, monkeypatch):
    br, fake = make_browser(recipe, monkeypatch)
    fake.results.append({'ok': False, 'error': 'TypeError: Failed to fetch'})
    with pytest.raises(urllib.error.URLError):
        br.open_novisit('https://www.economist.com/x')


def test_empty_reply_raises_rather_than_returning_nothing(recipe, monkeypatch):
    br, _ = make_browser(recipe, monkeypatch)      # FakeCDP returns value None
    with pytest.raises(urllib.error.URLError):
        br.open_novisit('https://www.economist.com/x')


@pytest.mark.parametrize('url,same', [
    ('https://www.economist.com/a', True),
    ('https://economist.com/a', True),
    ('https://cdn.economist.com/a', True),
    ('https://www.livemint.com/logo.png', False),
    ('https://evil-economist.com.attacker.net/x', False),
    ('not a url at all', False),
])
def test_same_origin_decides_which_transport_a_url_takes(recipe, url, same):
    assert recipe.CDPBrowser._same_origin(url) is same


def test_cross_origin_asset_bypasses_the_session(recipe, monkeypatch):
    """CORS blocks an in-page fetch off-origin, so those go out over urllib."""
    br, fake = make_browser(recipe, monkeypatch)
    called = {}
    monkeypatch.setattr(br, '_plain_open',
                        lambda url, timeout: called.setdefault('url', url))
    br.open_novisit('https://www.livemint.com/logo.png')
    assert called['url'] == 'https://www.livemint.com/logo.png'
    assert fake.calls == []          # the session was not used at all


def test_accepts_a_request_object_not_just_a_string(recipe, monkeypatch):
    br, fake = make_browser(recipe, monkeypatch, [page_result(b'ok')])

    class Req:
        def get_full_url(self):
            return 'https://www.economist.com/from-request'

    br.open_novisit(Req())
    assert 'from-request' in fake.calls[0][1]['expression']


def test_calibre_fetcher_surface_is_present(recipe, monkeypatch):
    """calibre calls these on whatever get_browser returns; a missing one
    surfaces only as a failed live download, which is why this is asserted."""
    br, fake = make_browser(recipe, monkeypatch)
    assert type(br).open is type(br).open_novisit
    assert br.clone_browser() is br          # one shared session, guarded by a lock
    assert br.is_method_ok('get') is True
    assert br.is_method_ok('POST') is False
    br.set_simple_cookie('a', 'b', '.economist.com')
    br.set_user_agent('anything')
    br.shutdown()
    assert fake.closed is True


# --------------------------------------------------------------------------
# get_content: no silent fallback
# --------------------------------------------------------------------------

def test_get_content_fails_loudly_when_no_session_is_served(recipe, monkeypatch):
    monkeypatch.setattr(recipe, 'cdp_browser', lambda *a, **k: None)
    with pytest.raises(recipe.CookiesUnusable) as excinfo:
        recipe.get_content('https://www.economist.com/x')
    assert '--serve' in str(excinfo.value)


def test_get_content_rejects_a_challenge_page(recipe, monkeypatch):
    class Br:
        def open_novisit(self, url, timeout=None):
            import io
            return io.BytesIO(b'<html>captcha-delivery</html>')

    monkeypatch.setattr(recipe, 'cdp_browser', lambda *a, **k: Br())
    with pytest.raises(recipe.CookiesUnusable):
        recipe.get_content('https://www.economist.com/x')


def test_get_content_returns_the_page_and_asks_for_the_app_view(recipe, monkeypatch):
    seen = {}

    class Br:
        def open_novisit(self, url, timeout=None):
            import io
            seen['url'] = url
            return io.BytesIO(b'<html>__NEXT_DATA__</html>')

    monkeypatch.setattr(recipe, 'cdp_browser', lambda *a, **k: Br())
    assert recipe.get_content('https://www.economist.com/x') == b'<html>__NEXT_DATA__</html>'
    assert seen['url'].startswith('https://www.economist.com/x?')
    assert 'webview=liskov' in seen['url']


# --------------------------------------------------------------------------
# Finding the session script (the path that gets imported and executed)
# --------------------------------------------------------------------------

def test_env_var_wins_over_the_pointer_file(recipe, monkeypatch, tmp_path):
    monkeypatch.setenv('ECONOMIST_SESSION_SCRIPT', '~/from/env.py')
    assert recipe._find_session_script() == os.path.expanduser('~/from/env.py')


def test_pointer_file_is_used_when_no_env_var(recipe, monkeypatch, tmp_path):
    monkeypatch.delenv('ECONOMIST_SESSION_SCRIPT', raising=False)
    monkeypatch.setattr(recipe, 'CONFIG_DIR', str(tmp_path))
    pointer = tmp_path / 'economist-session.path'
    pointer.write_text('/opt/economist/economist_session.py\n')
    os.chmod(pointer, 0o644)
    assert recipe._find_session_script() == '/opt/economist/economist_session.py'


@pytest.mark.parametrize('mode', [0o646, 0o664, 0o666])
def test_group_or_world_writable_pointer_is_refused(recipe, monkeypatch, tmp_path, mode):
    """The path in this file is imported and executed inside calibre, so a
    pointer anyone else can write is treated as absent rather than obeyed."""
    monkeypatch.delenv('ECONOMIST_SESSION_SCRIPT', raising=False)
    monkeypatch.setattr(recipe, 'CONFIG_DIR', str(tmp_path))
    pointer = tmp_path / 'economist-session.path'
    pointer.write_text(str(tmp_path / 'attacker.py') + '\n')
    os.chmod(pointer, mode)
    assert recipe._find_session_script() == ''


# --------------------------------------------------------------------------
# Article JSON to HTML
# --------------------------------------------------------------------------

def test_paragraph_prefers_html_then_json_then_plain_text(recipe):
    assert '<b>x</b>' in recipe.process_web_node(
        {'type': 'PARAGRAPH', 'textHtml': '<b>x</b>'})
    assert recipe.process_web_node(
        {'type': 'PARAGRAPH', 'text': 'plain'}).strip() == '<p>plain</p>'


def test_image_node_renders_source_alt_and_caption(recipe):
    html = recipe.process_web_node({
        'type': 'IMAGE', 'url': 'https://img/x.jpg', 'altText': 'alt text',
        'caption': {'textHtml': 'the caption'},
    })
    assert 'src="https://img/x.jpg"' in html
    assert 'alt text' in html and 'the caption' in html


def test_list_and_quote_nodes(recipe):
    assert recipe.process_web_list({'items': [{'text': 'one'}, {'text': 'two'}]}) == \
        '<ul><li>one</li><li>two</li></ul>'
    assert recipe.process_web_node(
        {'type': 'PULL_QUOTE', 'text': 'q'}) == '<blockquote>q</blockquote>'
    assert recipe.process_web_node({'type': 'DIVIDER'}) == '<hr>'


def test_unknown_node_type_is_skipped_not_fatal(recipe, capsys):
    """The site adds node types without warning; an article must not die on one.

    Actual behaviour: the node contributes nothing and its type is printed, so
    an unhandled type shows up in the download log rather than in a traceback.
    """
    assert recipe.process_web_node({'type': 'SOMETHING_NEW_2027'}) == ''
    assert 'SOMETHING_NEW_2027' in capsys.readouterr().out


def test_textjson_renders_nested_inline_markup(recipe):
    nodes = [{'type': 'bold', 'children': [{'type': 'text', 'value': 'bold bit'}]},
             {'type': 'text', 'value': ' and '},
             {'type': 'italic', 'children': [{'type': 'text', 'value': 'italic bit'}]}]
    assert recipe.parse_textjson(nodes) == '<b>bold bit</b> and <i>italic bit</i>'


def test_external_link_keeps_its_href(recipe):
    node = {'type': 'external_link', 'attributes': [{'name': 'href', 'value': 'https://e/x'}],
            'children': [{'type': 'text', 'value': 'link'}]}
    assert recipe.parse_textjson([node]) == '<a href="https://e/x">link</a>'


def test_safe_dict_returns_empty_rather_than_raising_on_a_missing_branch(recipe):
    data = {'a': {'b': {'c': 1}}}
    assert recipe.safe_dict(data, 'a', 'b') == {'c': 1}
    assert recipe.safe_dict(data, 'a', 'nope', 'deeper') == {}


def test_load_article_from_json_builds_a_full_article(recipe):
    raw = json.dumps({'props': {'pageProps': {'content': {
        'headline': 'The headline',
        'description': 'The rubric',
        'datePublishedString': 'September 5th 2026',
        'dateModified': '2026-09-05T10:00:00Z',
        'byline': 'Our correspondent',
        'body': [
            {'type': 'PARAGRAPH', 'text': 'First paragraph.'},
            {'type': 'CROSSHEAD', 'text': 'A crosshead'},
            {'type': 'PARAGRAPH', 'textHtml': 'Second <i>paragraph</i>.'},
        ],
    }}}})
    html = recipe.load_article_from_json(raw)
    assert 'The headline' in html
    assert 'First paragraph.' in html
    assert '<h4>A crosshead</h4>' in html
    assert 'Second <i>paragraph</i>.' in html
    assert html.startswith('<html><body><article>')


def test_load_article_from_json_raises_on_a_page_without_the_payload(recipe):
    """A challenge page reaching the parser must fail, not yield an empty article.

    KeyError is what the code raises today; asserting the specific type means a
    future silent-empty-article regression fails here.
    """
    with pytest.raises(KeyError):
        recipe.load_article_from_json(json.dumps({'props': {'pageProps': {}}}))


def test_non_http_asset_url_is_refused(recipe, monkeypatch):
    """Asset URLs come out of page HTML; urllib would open file:// happily."""
    br, _ = make_browser(recipe, monkeypatch)
    for url in ('file:///etc/passwd', 'ftp://host/x', 'javascript:alert(1)'):
        with pytest.raises(urllib.error.URLError):
            br._plain_open(url, timeout=5)
