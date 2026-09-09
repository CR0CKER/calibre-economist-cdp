"""Tests for the pure logic in check_economist_access.

Only cookie parsing and response classification are tested here: they are the
parts with real edge cases and no network. The transport probes are I/O against
a live, bot-protected site and are exercised by running the checker itself.

Run with: python3 -m pytest test_check_economist_access.py -q
"""

from __future__ import annotations

import pytest

from check_economist_access import classify, parse_cookie_header


def test_parses_a_plain_two_cookie_header() -> None:
    assert parse_cookie_header('a=1; b=2') == [('a', '1'), ('b', '2')]


def test_keeps_equals_signs_inside_the_value() -> None:
    # JWTs and base64 padding contain '=', so only the FIRST '=' separates
    # name from value. Splitting on every '=' would corrupt cf_clearance.
    assert parse_cookie_header('jwt=aaa.bbb=ccc==') == [('jwt', 'aaa.bbb=ccc==')]


def test_tolerates_irregular_whitespace_and_trailing_semicolon() -> None:
    assert parse_cookie_header('  a=1 ;b=2;  ') == [('a', '1'), ('b', '2')]


def test_skips_chunks_with_no_equals_rather_than_guessing() -> None:
    assert parse_cookie_header('a=1; garbage; b=2') == [('a', '1'), ('b', '2')]


def test_preserves_empty_value() -> None:
    # A cookie present but empty is meaningful; it must not be dropped.
    assert parse_cookie_header('a=; b=2') == [('a', ''), ('b', '2')]


def test_empty_header_yields_no_cookies() -> None:
    assert parse_cookie_header('') == []


@pytest.mark.parametrize(
    'marker',
    [b'captcha-delivery', b'Just a moment', b'_cf_chl_opt', b'Please enable JS'],
)
def test_classifies_each_interstitial_marker_as_blocked(marker: bytes) -> None:
    result = classify(b'<html>' + marker + b'</html>')
    assert result['ok'] is False
    assert 'blocked' in result['detail']


def test_classifies_next_data_page_as_ok() -> None:
    result = classify(b'<html><script id="__NEXT_DATA__">{}</script></html>')
    assert result['ok'] is True


def test_challenge_marker_wins_over_next_data() -> None:
    # A challenge page that happens to also mention __NEXT_DATA__ must never be
    # reported as success; the challenge check runs first for exactly this reason.
    result = classify(b'Just a moment __NEXT_DATA__')
    assert result['ok'] is False


def test_classifies_unrecognised_page_as_not_ok() -> None:
    result = classify(b'<html>something else entirely</html>')
    assert result['ok'] is False
    assert 'unexpected page' in result['detail']


# --- cookie freshness -----------------------------------------------------
# __cf_bm is the shortest-lived cookie in the jar (exactly 30 minutes) and
# embeds its issue time, so it is what really bounds the usable window.

def test_cf_bm_age_is_computed_from_the_embedded_timestamp() -> None:
    import time as _time

    from check_economist_access import cf_bm_age_minutes

    issued = int(_time.time()) - 600  # 10 minutes ago
    cookies = [('other', 'x'), ('__cf_bm', f'abc.def-{issued}.2732534-1.0.1.1-zzz')]
    age = cf_bm_age_minutes(cookies)
    assert age is not None
    assert 9.5 < age < 10.5


def test_cf_bm_age_is_none_when_cookie_absent() -> None:
    from check_economist_access import cf_bm_age_minutes

    assert cf_bm_age_minutes([('datadome', 'x')]) is None


def test_cf_bm_age_is_none_when_value_is_unparseable() -> None:
    from check_economist_access import cf_bm_age_minutes

    assert cf_bm_age_minutes([('__cf_bm', 'no-timestamp-here')]) is None
