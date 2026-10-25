"""Tests for the setup wizard. All cookie values here are synthetic."""

from __future__ import annotations

import pytest

import economist_session as session
import economist_setup as mod

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/155.0.0.0 Safari/537.36'
CURL = (f"curl 'https://www.economist.com/weeklyedition' \\\r\n"
        f"  -H 'user-agent: {UA}' \\\r\n"
        "  -H 'cookie: __cf_bm=t-1788376081.1-x; fcx_user=abc'\r\n")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(session, 'CONFIG_DIR', str(tmp_path / 'calibre'))
    monkeypatch.setattr(session, 'COOKIE_FILE', str(tmp_path / 'calibre' / 'economist_cookies.txt'))
    return tmp_path


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr('builtins.input', lambda prompt='': queue.pop(0))


# --- clipboard commands ------------------------------------------------------

def test_windows_reads_the_clipboard_through_powershell(monkeypatch):
    monkeypatch.setattr(mod.sys, 'platform', 'win32')
    cmd = mod.clipboard_command('paste')
    assert cmd[0].endswith('powershell.exe')
    assert 'Get-Clipboard -Raw' in cmd[-1]


def test_wayland_prefers_wl_paste(monkeypatch):
    monkeypatch.setattr(mod.sys, 'platform', 'linux')
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')
    monkeypatch.setattr(mod.shutil, 'which', lambda t: f'/usr/bin/{t}')
    assert mod.clipboard_command('paste') == ['/usr/bin/wl-paste', '--no-newline']
    assert mod.clipboard_command('copy') == ['/usr/bin/wl-copy']


def test_x11_falls_back_to_xclip(monkeypatch):
    monkeypatch.setattr(mod.sys, 'platform', 'linux')
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda t: '/usr/bin/xclip' if t == 'xclip' else None)
    assert mod.clipboard_command('paste') == ['/usr/bin/xclip', '-o', '-selection', 'clipboard']


def test_pbpaste_is_used_on_macos_and_only_there(monkeypatch):
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setattr(mod.shutil, 'which', lambda t: f'/usr/bin/{t}')
    monkeypatch.setattr(mod.sys, 'platform', 'darwin')
    assert mod.clipboard_command('copy') == ['/usr/bin/pbcopy']
    monkeypatch.setattr(mod.sys, 'platform', 'linux')
    assert mod.clipboard_command('copy') == ['/usr/bin/xclip', '-i', '-selection', 'clipboard']


def test_no_clipboard_tool_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(mod.sys, 'platform', 'linux')
    monkeypatch.setattr(mod.shutil, 'which', lambda t: None)
    assert mod.clipboard_command('paste') is None
    with pytest.raises(OSError):
        mod.read_clipboard()


# --- importing the session ---------------------------------------------------

def test_import_from_clipboard_writes_the_cookie_file(config, monkeypatch):
    monkeypatch.setattr(mod, 'read_clipboard', lambda: CURL)
    answers(monkeypatch, '')                      # "press Enter"
    assert mod.import_session(None) is True
    text = open(session.COOKIE_FILE, encoding='utf-8').read()
    assert f'USER_AGENT={UA}' in text
    assert 'COOKIE=__cf_bm=t-1788376081.1-x; fcx_user=abc' in text


def test_a_bad_paste_can_be_retried(config, monkeypatch):
    pastes = ['not a curl command', CURL]
    monkeypatch.setattr(mod, 'read_clipboard', lambda: pastes.pop(0))
    answers(monkeypatch, '', 'y', '')             # Enter, retry, Enter
    assert mod.import_session(None) is True
    assert not pastes


def test_giving_up_after_a_bad_paste_fails(config, monkeypatch):
    monkeypatch.setattr(mod, 'read_clipboard', lambda: 'nope')
    answers(monkeypatch, '', 'n')
    assert mod.import_session(None) is False


def test_an_existing_session_is_kept_when_asked(config, monkeypatch):
    (config / 'calibre').mkdir()
    (config / 'calibre' / 'economist_cookies.txt').write_text('USER_AGENT=old\n')
    answers(monkeypatch, 'n')
    assert mod.import_session(None) is True
    assert 'old' in open(session.COOKIE_FILE, encoding='utf-8').read()


def test_import_from_file_offers_to_shred_it(config, monkeypatch):
    src = config / 'eco-curl.txt'
    src.write_bytes(b'\xef\xbb\xbf' + CURL.encode())
    answers(monkeypatch, 'y')
    assert mod.import_session(str(src)) is True
    assert not src.exists()


def test_a_bad_file_is_offered_for_shredding_too(config, monkeypatch):
    """The importer shreds its input on a parse failure; the setup tool left
    the file, login and all, behind."""
    src = config / 'eco-curl.txt'
    src.write_text("curl ^\"https://x^\" -b ^\"__cf_bm=z^\"")
    answers(monkeypatch, 'y')
    assert mod.import_session(str(src)) is False
    assert not src.exists()


# --- the whole run -----------------------------------------------------------

def test_main_runs_all_four_steps(config, monkeypatch):
    done = []
    monkeypatch.setattr(mod, 'check_environment', lambda: done.append('check') or True)
    monkeypatch.setattr(mod, 'import_session', lambda f: done.append('import') or True)
    monkeypatch.setattr(mod, 'seed', lambda: done.append('seed') or True)
    monkeypatch.setattr(mod, 'hand_over_recipe', lambda: done.append('recipe'))
    assert mod.main([]) == 0
    assert done == ['check', 'import', 'seed', 'recipe']


def test_main_stops_when_seeding_fails(config, monkeypatch):
    monkeypatch.setattr(mod, 'check_environment', lambda: True)
    monkeypatch.setattr(mod, 'import_session', lambda f: True)
    monkeypatch.setattr(mod, 'seed', lambda: False)
    monkeypatch.setattr(mod, 'hand_over_recipe', lambda: pytest.fail('must not run'))
    assert mod.main([]) == 1
