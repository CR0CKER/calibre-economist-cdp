#!/usr/bin/env python3
"""One-time setup: import your browser session, seed the profile, hand over the recipe.

Walks through the README's Setup and recipe installation interactively:

1. checks Python, the browser and calibre's config folder
2. reads a "Copy as cURL" export straight from the clipboard (or --from-file)
   and writes the cookie file - no temporary file holding your login
3. seeds the dedicated browser profile (economist_session.py --seed)
4. puts economist.recipe on the clipboard for calibre's "Add a custom news source"

Usage::

    python3 economist_setup.py                   # Linux, macOS
    .venv\\Scripts\\python.exe economist_setup.py  # Windows
    python3 economist_setup.py --from-file eco-curl.txt

SECURITY: like the importer, this never prints a cookie value.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404 - argv lists only, never a shell
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import economist_chrome  # noqa: E402
import economist_session  # noqa: E402
import import_curl_cookies  # noqa: E402

RECIPE = os.path.join(HERE, 'economist.recipe')
# The same venv the recipe's _python() looks for; keep the two in step.
VENV_PYTHON = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------

# Per direction: the Windows PowerShell one-liner, then the first tool found
# on PATH. pb* tools are macOS's only option and count nowhere else; wl-* tools
# only count in a Wayland session.
CLIPBOARD = {
    'paste': (
        '[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Clipboard -Raw',
        [('pbpaste', []), ('wl-paste', ['--no-newline']),
         ('xclip', ['-o', '-selection', 'clipboard']), ('xsel', ['--clipboard', '--output'])],
    ),
    'copy': (
        '[Console]::InputEncoding = [Text.Encoding]::UTF8; '
        'Set-Clipboard -Value ([Console]::In.ReadToEnd())',
        [('pbcopy', []), ('wl-copy', []),
         ('xclip', ['-i', '-selection', 'clipboard']), ('xsel', ['--clipboard', '--input'])],
    ),
}


def clipboard_command(direction: str) -> list[str] | None:
    """argv that pastes to stdout or copies from stdin, or None if there is none."""
    script, tools = CLIPBOARD[direction]
    if sys.platform == 'win32':
        return economist_chrome.powershell(script)
    macos = sys.platform == 'darwin'
    wayland = bool(os.environ.get('WAYLAND_DISPLAY'))
    for name, args in tools:
        if name.startswith('pb') != macos or (name.startswith('wl-') and not wayland):
            continue
        path = shutil.which(name)
        if path:
            return [path, *args]
    return None


def read_clipboard() -> str:
    cmd = clipboard_command('paste')
    if not cmd:
        raise OSError('no clipboard tool found (install wl-clipboard or xclip, '
                      'or use --from-file)')
    result = subprocess.run(cmd, capture_output=True, timeout=15, check=False,
                            **economist_chrome.no_window())
    if result.returncode != 0:
        raise OSError('reading the clipboard failed')
    return result.stdout.decode('utf-8', 'replace')


def write_clipboard(text: str) -> bool:
    cmd = clipboard_command('copy')
    if not cmd:
        return False
    try:
        result = subprocess.run(cmd, input=text.encode('utf-8'), capture_output=True,
                                timeout=15, check=False, **economist_chrome.no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def ask(question: str, default: bool = True) -> bool:
    hint = '[Y/n]' if default else '[y/N]'
    answer = input(f'{question} {hint} ').strip().lower()
    return default if not answer else answer.startswith('y')


def step(n: int, title: str) -> None:
    print(f'\n[{n}/4] {title}', flush=True)


def check_environment() -> bool:
    """Report what was found; False if setup cannot continue."""
    ok = True
    if sys.version_info < (3, 10):
        print(f'  ERROR  Python {sys.version.split()[0]}: 3.10 or newer is needed')
        ok = False
    else:
        print(f'  ok     Python {sys.version.split()[0]}')

    browser = economist_chrome.BROWSER_CMD[0]
    if shutil.which(browser):
        print(f'  ok     browser {browser}')
    else:
        print(f'  ERROR  no browser at {browser}. Install Chrome or Chromium, '
              'or set ECONOMIST_BROWSER_CMD')
        ok = False

    config = economist_session.CONFIG_DIR
    if os.path.isdir(config):
        print(f'  ok     calibre config {config}')
    else:
        print(f'  note   {config} does not exist yet; it will be created '
              '(has calibre been started once?)')

    if sys.platform == 'win32' and not os.path.isfile(VENV_PYTHON):
        print('  note   no .venv here. calibre will run the helper with py/python '
              'from PATH; run "uv venv" in this folder to pin it')

    if sys.platform.startswith('linux') and os.path.isdir(economist_session.FLATPAK_CONFIG_DIR):
        print('  note   flatpak calibre: it needs permission to start the browser on the host')
        flatpak = shutil.which('flatpak')
        if flatpak and ask('         Grant it now (flatpak override --talk-name=org.freedesktop.Flatpak)?'):
            subprocess.run([flatpak, 'override', '--user',
                            '--talk-name=org.freedesktop.Flatpak',
                            'com.calibre_ebook.calibre'], check=False)
    return ok


CURL_HELP = f"""\
  In Chrome:
    1. Open https://www.economist.com/weeklyedition and log in.
    2. F12 -> Network, clear the filter box, click "Doc".
    3. Reload TWICE with Ctrl+Shift+R.
    4. Right-click the top document row -> Copy -> \
{'Copy as cURL (bash)' if sys.platform == 'win32' else 'Copy as cURL'}."""


def import_session(from_file: str | None) -> bool:
    """Parse the cURL export and write the cookie file. False on give-up."""
    if os.path.exists(economist_session.COOKIE_FILE) and not ask(
            '  A saved session already exists. Replace it?'):
        return True

    while True:
        if from_file:
            with open(from_file, encoding='utf-8', errors='replace') as f:
                text = f.read()
        else:
            print(CURL_HELP)
            input('  Press Enter once it is on the clipboard...')
            try:
                text = read_clipboard()
            except OSError as e:
                print(f'  ERROR  {e}')
                return False
        try:
            # parse_curl explains what is wrong on stderr and exits; catch that
            # so the user can copy again instead of starting over.
            harvest = import_curl_cookies.parse_curl(text)
            break
        except SystemExit:
            if from_file:
                offer_shred(from_file)
                return False
            if not ask('  Try copying again?'):
                return False

    import_curl_cookies.save_and_report(harvest, economist_session.COOKIE_FILE)
    if from_file:
        offer_shred(from_file)
    return True


def offer_shred(path: str) -> None:
    """Offer to shred an export file, whether or not it parsed: it may hold a login."""
    if ask(f'  Delete {path}? It may hold your login.'):
        import_curl_cookies.shred(path)


def seed() -> bool:
    print('  A browser window opens briefly. This takes up to a minute.', flush=True)
    rc = economist_session.main(['economist_session.py', '--seed'])
    if rc != 0:
        print('\n  Seeding failed. Wait a while before trying again: repeated '
              'attempts can earn a captcha.\n  If it said rt=c, log in again in '
              'your browser first. Re-run this tool to retry.')
    return rc == 0


def hand_over_recipe() -> None:
    with open(RECIPE, encoding='utf-8') as f:
        source = f.read()
    copied = write_clipboard(source)
    if copied:
        print('  ok     the recipe is on the clipboard (this also replaced the '
              'cURL export there)')
    else:
        print(f'  Copy the contents of {RECIPE} yourself.')
    print(f"""
  In calibre:
    1. Fetch news -> Add a custom news source -> Switch to advanced mode
    2. Delete the template, paste {'the clipboard' if copied else 'the recipe'}, \
title it "The Economist (CDP)"
    3. Add/Update recipe, then Download now (or schedule) THAT recipe""")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='One-time setup for the Economist recipe.')
    parser.add_argument('--from-file', metavar='PATH',
                        help='read the "Copy as cURL" export from a file instead of the clipboard')
    args = parser.parse_args(argv)

    step(1, 'Checking this machine')
    if not check_environment():
        return 1
    step(2, 'Importing your browser session')
    if not import_session(args.from_file):
        return 1
    step(3, 'Seeding the dedicated browser profile')
    if not seed():
        return 1
    step(4, 'Adding the recipe to calibre')
    hand_over_recipe()
    print('\nSetup done.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nAborted.')
        sys.exit(130)
