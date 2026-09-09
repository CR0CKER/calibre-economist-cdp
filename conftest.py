"""Make the recipe importable so it can be tested.

`economist.recipe` is Python, but calibre compiles it from a string at run time,
so it has no importable module name and it imports calibre's own packages, which
do not exist outside calibre. This installs minimal stubs for those imports and
loads the file as the module `economist_recipe`.

Deliberately *not* stubbed away: the recipe's own logic. The stubs stand in only
for what calibre provides.

Known gap: `html5_parser` and `lxml` are stubbed too, because a pip-installed
lxml and html5-parser disagree about libxml2 and cannot be imported together.
The index parser that uses them is therefore not covered here; everything that
does not need real HTML parsing is. Index parsing is exercised by a live
download (see README, Verification).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

RECIPE = Path(__file__).parent / 'economist.recipe'


def _install_stubs() -> None:
    if 'calibre' in sys.modules:
        return

    calibre = types.ModuleType('calibre')
    calibre.browser = lambda *a, **k: None            # unused since the CDP-only refactor
    utils = types.ModuleType('calibre.utils')
    date = types.ModuleType('calibre.utils.date')
    date.local_tz = None
    web = types.ModuleType('calibre.web')
    feeds = types.ModuleType('calibre.web.feeds')
    news = types.ModuleType('calibre.web.feeds.news')

    class BasicNewsRecipe:                            # the base class the recipe extends
        recipe_specific_options: dict = {}

        def __init__(self, *a, **k) -> None:
            pass

        def log(self, *a, **k) -> None:
            pass

        def get_browser(self, *a, **k):
            raise AssertionError('the recipe must not fall back to calibre browsers')

        def canonicalize_internal_url(self, url, is_link=True):
            return (url,)

        def publication_date(self):
            return None

    news.BasicNewsRecipe = BasicNewsRecipe

    html5 = types.ModuleType('html5_parser')
    html5.parse = lambda *a, **k: (_ for _ in ()).throw(
        NotImplementedError('html5_parser is stubbed in tests'))
    lxml = types.ModuleType('lxml')
    etree = types.ModuleType('lxml.etree')
    lxml.etree = etree

    for name, mod in [
        ('calibre', calibre), ('calibre.utils', utils), ('calibre.utils.date', date),
        ('calibre.web', web), ('calibre.web.feeds', feeds), ('calibre.web.feeds.news', news),
        ('html5_parser', html5), ('lxml', lxml), ('lxml.etree', etree),
    ]:
        sys.modules.setdefault(name, mod)


def _load_recipe() -> types.ModuleType:
    _install_stubs()
    module = types.ModuleType('economist_recipe')
    module.__file__ = str(RECIPE)
    sys.modules['economist_recipe'] = module
    exec(compile(RECIPE.read_text(), str(RECIPE), 'exec'), module.__dict__)  # noqa: S102
    return module


@pytest.fixture(scope='session')
def recipe() -> types.ModuleType:
    """The recipe loaded as a module, with calibre's imports stubbed."""
    return _load_recipe()
