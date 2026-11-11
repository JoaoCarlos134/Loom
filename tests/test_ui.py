"""The UI's side of the API contract, read from the files the app serves.

There is no build step and no JavaScript test runner, deliberately. What can be
checked without one is checked here: that the detail view asks for what it is
supposed to show, and that catalog text never meets ``innerHTML``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "src" / "recsys" / "ui"


@pytest.fixture(scope="module")
def app_js() -> str:
    return (UI / "app.js").read_text(encoding="utf-8")


def test_catalog_text_never_reaches_innerhtml(app_js):
    """Synopses and titles are third-party text. CLAUDE.md: textContent only."""
    assert not re.search(r"\.(innerHTML|outerHTML)\b|insertAdjacentHTML|document\.write", app_js)


def test_the_detail_view_asks_for_works_like_this_one(app_js):
    """/api/similar is the core feature, and the UI never called it."""
    assert re.search(r'"/api/similar/"\s*\+\s*encodeURIComponent', app_js)


def test_it_asks_both_models(app_js):
    """Content (what works are about) and cf (who liked what) fail differently;
    the row shows both, the second only where its corpus has an opinion."""
    assert "method=cf" in app_js


def test_it_suppresses_the_works_own_story_by_default(app_js):
    """The API suppresses the query's adaptations, sequels and volumes unless
    asked otherwise; the UI must not ask otherwise."""
    assert "same_franchise=true" not in app_js


def test_it_asks_across_domains(app_js):
    """No domain filter on the request: an anime's nearest book is the point."""
    similar_calls = re.findall(r'"/api/similar/"[^;]*;', app_js)
    assert similar_calls and not any("domain=" in call for call in similar_calls)


def test_the_tmdb_notice_is_shown_in_the_app():
    """TMDB's terms require this exact notice where the app credits its sources."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    notice = ("This product uses TMDB and the TMDB APIs but is not endorsed, certified, "
              "or otherwise approved by TMDB.")
    assert notice in " ".join(html.split())


def test_titles_can_be_shown_in_english(app_js):
    """One switch, remembered: Kimetsu no Yaiba becomes Demon Slayer."""
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="title-lang"' in html
    assert "item.title_english" in app_js and "englishTitles" in app_js
    # The domain toggles must not pick the setting up as a domain.
    assert '"#domains .chip[data-domain]"' in app_js
