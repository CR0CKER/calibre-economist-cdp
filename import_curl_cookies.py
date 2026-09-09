#!/usr/bin/env python3
"""Import Economist cookies from a Chrome DevTools "Copy as cURL" command.

Copying the Cookie header by hand is fiddly and easy to truncate. DevTools can
emit the whole request as a shell command instead, which carries both the
User-Agent and the full cookie jar - including HttpOnly cookies like
cf_clearance, which JavaScript cannot read.

Usage::

    # In DevTools: Network -> Doc -> reload -> right-click the document row
    #   -> Copy -> Copy as cURL
    wl-paste > /run/user/$(id -u)/eco-curl.txt
    python3 import_curl_cookies.py /run/user/$(id -u)/eco-curl.txt

SECURITY: this script never prints cookie values. It reports names and value
lengths only, writes the target file with mode 600, and shreds its input.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import NoReturn, TypedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from economist_session import COOKIE_FILE  # noqa: E402  same config-dir logic everywhere


# Chrome emits bash ANSI-C quoting -- $'...' -- for any header or cookie value
# needing an escape. Salesforce session ids contain '!', which Chrome writes as
# \u0021, so this is hit in practice. shlex does not understand $'...': it keeps
# the '$' and leaves the escape undecoded, silently corrupting the credential.
_ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)

_ANSI_C_SIMPLE = {
    'a': '\a', 'b': '\b', 'e': '\x1b', 'E': '\x1b', 'f': '\f',
    'n': '\n', 'r': '\r', 't': '\t', 'v': '\v',
    '\\': '\\', "'": "'", '"': '"', '?': '?',
}


def decode_ansi_c(body: str) -> str:
    """Decode the escape sequences bash recognises inside $'...'."""
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != '\\' or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
        elif nxt == 'x':
            m = re.match(r'[0-9a-fA-F]{1,2}', body[i + 2:])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + m.end()
            else:
                out.append(nxt)
                i += 2
        elif nxt in 'uU':
            width = 4 if nxt == 'u' else 8
            m = re.match(r'[0-9a-fA-F]{1,%d}' % width, body[i + 2:])
            if m:
                out.append(chr(int(m.group(), 16)))
                i += 2 + m.end()
            else:
                out.append(nxt)
                i += 2
        elif nxt.isdigit():
            m = re.match(r'[0-7]{1,3}', body[i + 1:])
            if m:
                out.append(chr(int(m.group(), 8)))
                i += 1 + m.end()
            else:
                out.append(nxt)
                i += 2
        else:
            # Unknown escape: bash keeps the backslash and the character.
            out.append(ch)
            out.append(nxt)
            i += 2
    return ''.join(out)


def expand_ansi_c_quotes(text: str) -> str:
    """Rewrite $'...' runs into ordinary quoting that shlex parses correctly."""
    return _ANSI_C_QUOTED.sub(lambda m: shlex.quote(decode_ansi_c(m.group(1))), text)


class Harvest(TypedDict):
    """The two values the recipe needs, pulled from one browser request."""

    user_agent: str
    cookie_header: str


def fail(message: str) -> NoReturn:
    print(f'ERROR: {message}', file=sys.stderr)
    raise SystemExit(2)


def parse_curl(text: str) -> Harvest:
    """Extract the user-agent and cookie header from a copied cURL command.

    DevTools emits POSIX-shell quoting, so shlex handles the escaping (including
    the '\\'' sequences that appear inside cookie values) correctly.
    """
    text = text.strip()
    if not text.startswith('curl'):
        fail('that does not look like a "Copy as cURL" command (it must start with "curl")')

    # Join shell line continuations, then normalise ANSI-C quoting, before
    # tokenising.
    tokens = shlex.split(expand_ansi_c_quotes(text.replace('\\\n', ' ')))

    headers: dict[str, str] = {}
    cookie_from_b = ''
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ('-H', '--header') and i + 1 < len(tokens):
            name, _, value = tokens[i + 1].partition(':')
            headers[name.strip().lower()] = value.strip()
            i += 2
            continue
        if tok in ('-b', '--cookie') and i + 1 < len(tokens):
            cookie_from_b = tokens[i + 1].strip()
            i += 2
            continue
        i += 1

    user_agent = headers.get('user-agent', '')
    cookie_header = headers.get('cookie', '') or cookie_from_b

    if not user_agent:
        fail('no user-agent header found in the cURL command')
    if not cookie_header:
        fail(
            'no cookie header found. You are probably not logged in, or you copied '
            'a request that carries no cookies - copy the main document request '
            '(Network -> Doc -> the top row) rather than an asset or XHR.'
        )
    if '__cf_bm=' not in cookie_header:
        # __cf_bm is set by Cloudflare on a fresh response and lives 30 minutes.
        # Its absence means the copied request predates a re-issue - i.e. a stale
        # row in the Network log - and the fetch will be challenged. Fail here
        # rather than after a slow download attempt.
        fail(
            'the copied request has no __cf_bm cookie, so it is a stale entry from '
            'the Network log and will be blocked.\n'
            'Clear the Network log, hard-reload the page (Ctrl+Shift+R), then copy '
            'the NEW top document row.'
        )
    return {'user_agent': user_agent, 'cookie_header': cookie_header}


def summarise(cookie_header: str) -> list[tuple[str, int]]:
    """Cookie names and value lengths - never the values themselves."""
    out: list[tuple[str, int]] = []
    for chunk in cookie_header.split(';'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        name, _, value = chunk.partition('=')
        if name.strip():
            out.append((name.strip(), len(value.strip())))
    return out


def write_cookie_file(harvest: Harvest, path: str = COOKIE_FILE) -> None:
    """Write the cookie file atomically with mode 600."""
    content = (
        '# The Economist browser cookies for the calibre recipe.\n'
        '# CREDENTIALS - keep chmod 600, never commit.\n'
        '# Written by import_curl_cookies.py. Re-run that to refresh.\n'
        '\n'
        f'USER_AGENT={harvest["user_agent"]}\n'
        f'COOKIE={harvest["cookie_header"]}\n'
    )
    tmp = path + '.tmp'
    # Create with restrictive permissions from the outset, so the secret is
    # never briefly world-readable on disk.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def shred(path: str) -> None:
    """Overwrite then remove the input file so the cookies do not linger."""
    try:
        size = os.path.getsize(path)
        with open(path, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        os.remove(path)
    except OSError as e:
        print(f'WARNING: could not shred {path}: {e}', file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = argv[1]
    if not os.path.exists(src):
        fail(f'input file not found: {src}')

    # Shred the input whatever happens: on the failure paths it still holds a
    # full set of live session cookies.
    try:
        with open(src, encoding='utf-8', errors='replace') as f:
            harvest = parse_curl(f.read())
    except BaseException:
        shred(src)
        raise

    write_cookie_file(harvest)
    cookies = summarise(harvest['cookie_header'])

    print(f'Wrote {COOKIE_FILE} (mode 600)')
    print(f'User-Agent: {harvest["user_agent"]}')
    print(f'Cookies: {len(cookies)} (names and lengths only)')
    for name, length in cookies:
        print(f'    {name:<32s} len={length}')
    present = {n for n, _ in cookies}
    for wanted in ('cf_clearance', 'datadome'):
        print(f'    -> {wanted}: {"present" if wanted in present else "MISSING"}')

    shred(src)
    print(f'Shredded {src}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
