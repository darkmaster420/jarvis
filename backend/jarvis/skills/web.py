"""Web-related skills: open URL, search."""
from __future__ import annotations

import urllib.parse
import webbrowser

from .base import SkillResult


def open_url(url: str) -> SkillResult:
    if not url:
        return SkillResult("Which site?", intent="open_url", success=False)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url, new=2)
        return SkillResult(f"Opening {url}.", intent="open_url")
    except Exception as e:
        return SkillResult(f"Couldn't open browser: {e}",
                           intent="open_url", success=False)


def search(query: str) -> SkillResult:
    q = query.strip()
    if not q:
        return SkillResult("What should I search for?",
                           intent="web_search", success=False)
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)
    webbrowser.open(url, new=2)
    return SkillResult(f"Searching for {q}.", intent="web_search")


def close_browser_tab() -> SkillResult:
    """Send Ctrl+W to the focused window (browsers, many editors) — closes the active tab."""
    from . import desktop

    return desktop.key_combo("ctrl+w")


def reopen_closed_browser_tab() -> SkillResult:
    """Send Ctrl+Shift+T to reopen the last closed tab in typical browsers."""
    from . import desktop

    return desktop.key_combo("ctrl+shift+t")
