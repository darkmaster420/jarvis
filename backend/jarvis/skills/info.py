"""Info skills: time, date, system stats, weather."""
from __future__ import annotations

import datetime as _dt
import logging
import time

import psutil
import requests

from .base import SkillResult

log = logging.getLogger(__name__)

_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_WEATHER_TTL = 600.0


def time_now() -> SkillResult:
    now = _dt.datetime.now().strftime("%I:%M %p").lstrip("0")
    return SkillResult(f"It's {now}.", intent="time")


def date_today() -> SkillResult:
    today = _dt.datetime.now().strftime("%A, %B %d, %Y")
    return SkillResult(f"Today is {today}.", intent="date")


def system_stats() -> SkillResult:
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory().percent
    parts = [f"CPU at {cpu:.0f} percent", f"memory at {mem:.0f} percent"]
    if psutil.sensors_battery():
        b = psutil.sensors_battery()
        state = "charging" if b.power_plugged else "on battery"
        parts.append(f"battery {b.percent:.0f} percent ({state})")
    return SkillResult(", ".join(parts) + ".", intent="sys_stats")


def weather(location: str = "") -> SkillResult:
    loc = location.strip() or ""
    key = loc.lower()
    hit = _WEATHER_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _WEATHER_TTL:
        return SkillResult(hit[1], intent="weather")
    try:
        url = f"https://wttr.in/{loc}?format=%C+%t+feels+like+%f"
        r = requests.get(url, timeout=4)
        r.raise_for_status()
        txt = r.text.strip()
        if not txt or "Unknown" in txt:
            return SkillResult("I couldn't get the weather.",
                               intent="weather", success=False)
        msg = f"Currently: {txt}." if not loc else f"Weather in {loc}: {txt}."
        _WEATHER_CACHE[key] = (time.monotonic(), msg)
        return SkillResult(msg, intent="weather")
    except Exception as e:
        log.warning("weather failed: %s", e)
        return SkillResult("I couldn't reach the weather service.",
                           intent="weather", success=False)
