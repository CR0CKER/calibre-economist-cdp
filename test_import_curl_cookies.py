"""Tests for the DevTools cURL importer.

All fixtures use synthetic cookie values; no real credentials appear here.
"""

from __future__ import annotations

import os

import pytest

import import_curl_cookies as mod
from import_curl_cookies import parse_curl, summarise, write_cookie_file

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'


def curl_cmd(cookie: str = '__cf_bm=t-1788376081.1-x; cf_clearance=abc123; datadome=xyz789') -> str:
    return (
        "curl 'https://www.economist.com/weeklyedition' "
        f"-H 'user-agent: {UA}' "
        f"-H 'cookie: {cookie}' "
        "-H 'accept: text/html' --compressed"
    )


def test_extracts_user_agent_and_cookie() -> None:
    got = parse_curl(curl_cmd())
    assert got['user_agent'] == UA
    assert got['cookie_header'] == '__cf_bm=t-1788376081.1-x; cf_clearance=abc123; datadome=xyz789'


def test_header_names_are_matched_case_insensitively() -> None:
    cmd = f"curl 'https://x' -H 'User-Agent: {UA}' -H 'Cookie: __cf_bm=z; a=1'"
    got = parse_curl(cmd)
    assert got['user_agent'] == UA
    assert got['cookie_header'] == '__cf_bm=z; a=1'


def test_accepts_dash_b_cookie_form() -> None:
    cmd = f"curl 'https://x' -H 'user-agent: {UA}' -b '__cf_bm=z; a=1; b=2'"
    assert parse_curl(cmd)['cookie_header'] == '__cf_bm=z; a=1; b=2'


def test_handles_shell_escaped_quote_inside_a_cookie_value() -> None:
    # DevTools escapes an embedded single quote as '\'' - shlex must unwind it
    # rather than truncating the cookie at that point.
    cmd = f"""curl 'https://x' -H 'user-agent: {UA}' -H 'cookie: __cf_bm=z; a=va'\\''lue; b=2'"""
    assert parse_curl(cmd)['cookie_header'] == "__cf_bm=z; a=va'lue; b=2"


def test_handles_line_continuations() -> None:
    cmd = f"curl 'https://x' \\\n  -H 'user-agent: {UA}' \\\n  -H 'cookie: __cf_bm=z; a=1'"
    assert parse_curl(cmd)['user_agent'] == UA


def test_rejects_input_that_is_not_a_curl_command() -> None:
    with pytest.raises(SystemExit):
        parse_curl('cf_clearance=abc123; datadome=xyz')


def test_rejects_request_carrying_no_cookies() -> None:
    with pytest.raises(SystemExit):
        parse_curl(f"curl 'https://x' -H 'user-agent: {UA}'")


def test_summarise_reports_lengths_never_values() -> None:
    rows = summarise('cf_clearance=abcdef; datadome=12345')
    assert rows == [('cf_clearance', 6), ('datadome', 5)]
    assert all(isinstance(n, str) and isinstance(v, int) for n, v in rows)


def test_written_file_is_mode_600_and_round_trips(tmp_path, monkeypatch) -> None:
    target = tmp_path / 'cookies.txt'
    write_cookie_file(
        {'user_agent': UA, 'cookie_header': 'cf_clearance=abc; datadome=xyz'},
        str(target),
    )
    assert oct(os.stat(target).st_mode)[-3:] == '600'

    # The checker must be able to read back exactly what the importer wrote.
    monkeypatch.syspath_prepend(os.path.dirname(os.path.abspath(mod.__file__)))
    from check_economist_access import load_credentials

    ua, cookies = (
        lambda c: (c['user_agent'], c['cookies'])
    )(load_credentials(str(target)))
    assert ua == UA
    assert cookies == [('cf_clearance', 'abc'), ('datadome', 'xyz')]


def test_shred_removes_the_input_file(tmp_path) -> None:
    src = tmp_path / 'curl.txt'
    src.write_text('curl secret')
    mod.shred(str(src))
    assert not src.exists()


# --- ANSI-C quoting -------------------------------------------------------
# Chrome emits `-b $'...'` (bash ANSI-C quoting) whenever a cookie value needs
# an escape, which happens in practice: Salesforce session ids contain '!',
# which Chrome writes as !. Plain shlex leaves both the '$' prefix and the
# escape sequence in place, silently corrupting the credential.

def test_decodes_ansi_c_quoted_cookie_with_unicode_escape() -> None:
    cmd = (
        f"curl 'https://x' -H 'user-agent: {UA}' "
        r"-b $'__cf_bm=z; fcx_access_token=00D3z!AQEA; b=2'"
    )
    assert parse_curl(cmd)['cookie_header'] == '__cf_bm=z; fcx_access_token=00D3z!AQEA; b=2'


def test_decodes_ansi_c_escapes_backslash_and_quote() -> None:
    cmd = (
        f"curl 'https://x' -H 'user-agent: {UA}' "
        r"-b $'__cf_bm=z; a=back\\slash; b=quo\'te'"
    )
    assert parse_curl(cmd)['cookie_header'] == "__cf_bm=z; a=back\\slash; b=quo'te"


def test_decodes_ansi_c_hex_escape() -> None:
    cmd = f"curl 'https://x' -H 'user-agent: {UA}' " + r"-b $'__cf_bm=z; a=x\x21y'"
    assert parse_curl(cmd)['cookie_header'] == '__cf_bm=z; a=x!y'


def test_plain_quoted_value_is_left_untouched() -> None:
    # Ordinary single quotes are not ANSI-C quoting, so the value must pass
    # through byte-for-byte with no escape processing.
    cmd = f"curl 'https://x' -H 'user-agent: {UA}' " + r"-b '__cf_bm=z; a=lit!eral'"
    assert parse_curl(cmd)['cookie_header'] == r'__cf_bm=z; a=lit!eral'


def test_rejects_a_stale_copy_with_no_cf_bm_cookie() -> None:
    # __cf_bm is issued fresh by Cloudflare and lives 30 minutes; its absence
    # means a stale Network-log row that will be challenged.
    cmd = f"curl 'https://x' -H 'user-agent: {UA}' -b 'datadome=abc; a=1'"
    with pytest.raises(SystemExit):
        parse_curl(cmd)


def test_input_is_shredded_even_when_parsing_fails(tmp_path) -> None:
    # The failure paths still hold a full set of live session cookies, so the
    # input must not survive a rejected import.
    src = tmp_path / 'curl.txt'
    src.write_text("curl 'https://x' -H 'user-agent: u' -b 'datadome=abc'")
    with pytest.raises(SystemExit):
        mod.main(['import_curl_cookies.py', str(src)])
    assert not src.exists()
