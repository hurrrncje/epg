"""Generate cleaned XMLTV listings for dynamic live-event and football channels."""

from __future__ import annotations

import ast
import hashlib
import gzip
import json
import os
import re
import time
import unicodedata
from datetime import datetime, time as datetime_time, timedelta, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types


EPG_URL = "https://opop.pro/JKmjctL4MbfezAX"
OUTPUT_GZ_FILE = Path("cleaned_epg.xml.gz")
AI_CACHE_FILE = Path("ai_cache.json")
GEMINI_MODEL = "gemini-3.8-flash"
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
AI_CACHE_VERSION = 1
CACHE_FAILURE_COOLDOWN = timedelta(hours=6)
AI_CACHE_MAX_AGE = timedelta(days=30)
GZIP_COMPRESSLEVEL = 6
BATCH_SIZE = 50
DESCRIPTION_BATCH_SIZE = 25
TARGET_CHANNELS = (
    {f"LIVE EVENT {number:02d}" for number in range(1, 37)}
    | {f"[PPV EVENT {number:02d}]" for number in range(37, 41)}
)
MAX_RETRIES = 3
REQUEST_TIMEOUT = (10, 120)
EASTERN = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
GMT = UTC
TIME_PATTERN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
LIVE_EVENT_PREFIX_PATTERN = re.compile(
    r"^\s*(?:LIVE\s+EVENT\s+\d{1,2}|\[?\s*PPV\s+EVENT\s+\d{1,2}\]?)"
    r"\s*(?:[-:–—]+\s*)?",
    re.IGNORECASE,
)
DAZN_LABEL_PREFIX_PATTERN = re.compile(
    r"^\s*DAZN\s+UK\s*\d{1,3}\s*:\s*", re.IGNORECASE
)
LEADING_EVENT_TIME_PATTERN = re.compile(
    r"^\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b\s*[-:–—]*\s*", re.IGNORECASE
)
TRAILING_GMT_TIME_PATTERN = re.compile(
    r"\s+at\s+(\d{1,2}):(\d{2})\s+GMT\s*$", re.IGNORECASE
)
NEXT_GAME_PATTERN = re.compile(
    r"Next Game:\s*(?P<away>.+?)\s+@\s+(?P<home>.+?)\s+on\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+at\s+"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*[AP]M)\s+"
    r"(?P<zone>EDT|EST|GMT|UTC)\b",
    re.IGNORECASE,
)
MATCHUP_PATTERN = re.compile(
    r"^(?P<prefix>.*?)(?P<away>[^@\n]+?)\s+@\s+"
    r"(?P<home>[^@\n]+?)(?P<suffix>\s+on\s+.*)?$",
    re.IGNORECASE,
)
EPL_EFL_CHANNEL_PATTERN = re.compile(
    r"^(?P<league>EPL|EFL)\s*(?P<number>0?[1-9]|[12]\d|3[0-5]):\s*"
    r"(?P<details>.*)$",
    re.IGNORECASE,
)
EUROPEAN_LEAGUE_CHANNEL_PATTERN = re.compile(
    r"^(?P<league>Bundesliga|La\s*Liga|LaLiga|Serie\s*A|Ligue\s*1|Ligue1)\s*"
    r"(?P<number>0?[1-9]|10):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
EPL_FIXTURE_PATTERN = re.compile(
    r"^(?P<home>.+?)\s+(?P<clock>(?:[01]?\d|2[0-3]):[0-5]\d)\s+"
    r"(?P<away>.+?)\s+(?P<day>\d{1,2})/(?P<month>\d{1,2})"
    r"(?:/(?P<year>\d{4}))?$"
)
EUROPEAN_LEAGUE_FIXTURE_PATTERN = re.compile(
    r"^(?P<matchup>.+?)\s*@\s*(?P<date>\d{1,2}\s+[A-Za-z]{3})"
    r"(?:\s+(?P<year>\d{4}))?\s+"
    r"(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\s+"
    r"(?P<zone>London|UK|BST|GMT|UTC)$",
    re.IGNORECASE,
)
MLS_CHANNEL_PATTERN = re.compile(
    r"^MLS\s*(?P<number>0?[1-9]|[1-4]\d|50):?\s*(?P<details>.*)$",
    re.IGNORECASE,
)
MLS_FIXTURE_PATTERN = re.compile(
    r"^(?P<home>.+?)\s+(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\s+"
    r"(?P<away>.+?)\s+(?P<date>\d{1,2}/\d{1,2})(?:/(?P<year>\d{4}))?$",
    re.IGNORECASE,
)
MLS_AT_FIXTURE_PATTERN = re.compile(
    r"^(?P<matchup>.+?)\s*@\s*(?P<date>\d{1,2}\s+[A-Za-z]{3})"
    r"(?:\s+(?P<year>\d{4}))?\s+"
    r"(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\s*(?P<zone>ET|EST|EDT)$",
    re.IGNORECASE,
)
ESPN_LIGUE1_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard"
)
LIGUE1_TEAM_ALIASES = {
    "aj auxerre": "auxerre",
    "angers sco": "angers",
    "estac troyes": "troyes",
    "fc lorient": "lorient",
    "havre ac": "le havre",
    "le havre ac": "le havre",
    "le mans fc": "le mans",
    "lille osc": "lille",
    "losc": "lille",
    "olympique de marseille": "marseille",
    "rc strasbourg": "strasbourg",
    "rc strasbourg alsace": "strasbourg",
}
LEAGUE_NAME_PREFIX_PATTERN = re.compile(
    r"^(?:Bundesliga|La\s*Liga|LaLiga|Serie\s*A|Ligue\s*1)\s*:?\s*",
    re.IGNORECASE,
)
LEAGUE_MATCHUP_PATTERN = re.compile(r"^(?P<home>.+?)\s+vs\.?\s+(?P<away>.+?)$", re.IGNORECASE)
PARAMOUNT_CHANNEL_PATTERN = re.compile(
    r"^Paramount\+\s*(?P<number>0?[1-9]|[1-9]\d|100):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
DISNEY_EVENT_CHANNEL_PATTERN = re.compile(
    r"^Disney\+\s*Events?\s*(?P<number>\d{1,3}):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
ESPN_EVENT_CHANNEL_PATTERN = re.compile(
    r"^ESPN\+\s*(?P<number>\d{1,3}):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
MAX_UK_CHANNEL_PATTERN = re.compile(
    r"^MAX\s+UK\s*(?P<number>0?[1-9]|[1-9]\d|100):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
DAZN_CHANNEL_PATTERN = re.compile(
    r"^DAZN\s+UK\s*(?P<number>0?[1-9]|[1-9]\d|100):\s*(?P<details>.*)$",
    re.IGNORECASE,
)
EPL_EVENT_DURATION = timedelta(minutes=150)
PARAMOUNT_FIXTURE_PATTERN = re.compile(
    r"^(?P<title>.+?)\s+@\s+(?P<date>\d{1,2}\s+[A-Za-z]{3})"
    r"(?:\s+(?P<year>\d{4}))?\s+"
    r"(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\s+(?P<zone>ET|EDT|EST)$",
    re.IGNORECASE,
)
SPORTS_EVENT_FIXTURE_PATTERN = re.compile(
    r"^(?P<title>.+?)\s+@\s*(?P<date>"
    r"\d{1,2}\s+[A-Za-z]{3}|[A-Za-z]{3}\s+\d{1,2})\s+"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*[AP]M)\s+"
    r"(?P<zone>ET|EDT|EST|London|UK|BST|GMT|UTC)$",
    re.IGNORECASE,
)
DAZN_FIXTURE_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*@\s*(?P<date>\d{1,2}\s+[A-Za-z]{3})"
    r"(?:\s+(?P<year>\d{4}))?\s+"
    r"(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\s+London$",
    re.IGNORECASE,
)


def _event_number_and_suffix(display_name: str) -> tuple[str, str] | None:
    """Return a supported event channel's stable name and changing suffix."""
    match = re.match(r"^(LIVE EVENT \d{2})(?=$|[\s-])(.*)$", display_name.strip(), re.IGNORECASE)
    if match:
        core = match.group(1).upper()
        if core in TARGET_CHANNELS:
            return core, match.group(2).strip().lstrip("-:–— ").strip()

    ppv_match = re.match(
        r"^\[?\s*(?P<core>PPV EVENT (?P<number>3[7-9]|40))\]?"
        r"(?=$|[\]\s:–—-])(?P<suffix>.*)$",
        display_name.strip(),
        re.IGNORECASE,
    )
    if not ppv_match:
        return None
    core = f"[PPV EVENT {int(ppv_match.group('number')):02d}]"
    if core not in TARGET_CHANNELS:
        return None
    return core, ppv_match.group("suffix").strip().lstrip("-:–— ]").strip()


def _is_no_event_title(title: str) -> bool:
    cleaned = _clean_live_event_title(title).strip(" []-:–—").casefold()
    return cleaned in {"", "no event", "no match", "no game", "no event today"}


def _clean_live_event_title(title: str) -> str:
    """Remove a dynamic channel label and kickoff clock from an event title."""
    cleaned = LIVE_EVENT_PREFIX_PATTERN.sub("", title.strip(), count=1)
    cleaned = DAZN_LABEL_PREFIX_PATTERN.sub("", cleaned, count=1)
    cleaned = LEADING_EVENT_TIME_PATTERN.sub("", cleaned, count=1)
    return cleaned.strip(" -:–—")


def _format_gmt_time(value: datetime) -> str:
    """Format a GMT clock as 2pm or 2.30pm for display in programme titles."""
    local = value.astimezone(GMT)
    hour = local.hour % 12 or 12
    minute = f".{local.minute:02d}" if local.minute else ""
    meridiem = "am" if local.hour < 12 else "pm"
    return f"{hour}{minute}{meridiem}"


def _format_title_event_name(title: str) -> str:
    """Clean a title and render any trailing GMT clock in compact 12-hour form."""
    cleaned = _clean_live_event_title(title)
    match = TRAILING_GMT_TIME_PATTERN.search(cleaned)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            when = datetime(2000, 1, 1, hour, minute, tzinfo=GMT)
            cleaned = cleaned[:match.start()] + " at " + _format_gmt_time(when)
    return cleaned.strip()


def _parse_eastern_starts(suffix: str) -> list[datetime_time]:
    """Read 12-hour start times, including paired labels such as 5:25/8pm."""
    starts: list[datetime_time] = []
    for match in TIME_PATTERN.finditer(suffix):
        meridiem = match.group(3).lower()
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            starts.append(datetime_time(hour % 12 + (12 if meridiem == "pm" else 0), minute))

        # If the preceding slash-separated time omitted AM/PM, inherit the
        # suffix from the explicit time, e.g. 5:25/8pm means 5:25pm and 8pm.
        preceding = suffix[:match.start()]
        paired = re.search(r"(\d{1,2})(?::(\d{2}))?\s*/\s*$", preceding)
        if paired:
            first_hour = int(paired.group(1))
            first_minute = int(paired.group(2) or "0")
            if 1 <= first_hour <= 12 and 0 <= first_minute <= 59:
                first = datetime_time(
                    first_hour % 12 + (12 if meridiem == "pm" else 0), first_minute
                )
                starts.insert(max(0, len(starts) - 1), first)
    return list(dict.fromkeys(starts))


def _format_next_game(match: re.Match[str]) -> str | None:
    """Convert a next-game date/time into a GMT label, honoring EDT or EST."""
    clock = re.sub(r"\s+", "", match.group("clock")).upper()
    date_text = match.group("date")
    try:
        fmt = "%Y-%m-%d %I:%M%p" if ":" in clock else "%Y-%m-%d %I%p"
        local_time = datetime.strptime(f"{date_text} {clock}", fmt)
        zone = match.group("zone").upper()
        if zone in {"EDT", "EST"}:
            # The abbreviation distinguishes the repeated hour at DST fall-back.
            candidates = [
                local_time.replace(tzinfo=EASTERN, fold=fold)
                for fold in (0, 1)
            ]
            local_time = next((dt for dt in candidates if dt.tzname() == zone), None)
            if local_time is None:
                return None
        else:
            local_time = local_time.replace(tzinfo=GMT)
        gmt_time = local_time.astimezone(GMT)
        return (
            f"Next Game: {match.group('home').strip()} vs {match.group('away').strip()} "
            f"on {gmt_time.strftime('%d/%m/%Y')} at {gmt_time.strftime('%H:%M')} GMT"
        )
    except ValueError:
        return None


def _rewrite_football_text(text: str) -> str:
    """Put the home team first and convert a supported Next Game timestamp."""
    # Some provider descriptions contain JSON-style Unicode escapes literally
    # (for example, ``\u0026`` instead of ``&``). Decode them for display.
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    next_game = NEXT_GAME_PATTERN.search(text)
    if next_game:
        formatted = _format_next_game(next_game)
        if formatted:
            return text[: next_game.start()] + formatted + text[next_game.end() :]

    matchup = MATCHUP_PATTERN.match(text.strip())
    if not matchup:
        return text
    suffix = matchup.group("suffix") or ""
    return (
        matchup.group("prefix")
        + f"{matchup.group('home').strip()} vs {matchup.group('away').strip()}"
        + suffix
    )


def _replace_game_with_match(title: str) -> str:
    """Use football terminology in EPL/EFL programme titles."""
    replacements = {"game": "match", "Game": "Match", "GAME": "MATCH"}
    return re.sub(
        r"\bgame\b",
        lambda match: replacements.get(match.group(0), "match"),
        title,
        flags=re.IGNORECASE,
    )


def _parse_epl_dynamic_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse numbered EPL/EFL labels such as `EFL01: Team A 12:30 Team B 24/10`."""
    channel_match = EPL_EFL_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None
    number = int(channel_match.group("number"))
    league = channel_match.group("league").upper()
    if (league == "EPL" and number > 10) or (league == "EFL" and number > 35):
        return None
    details = channel_match.group("details").strip()
    result: dict[str, Any] = {
        "channel_name": f"{league}{number:02d}",
        "league": league,
        "title": "",
        "start_utc": None,
    }
    fixture = EPL_FIXTURE_PATTERN.match(details)
    if not fixture:
        return result

    year = int(fixture.group("year") or today.year)
    month = int(fixture.group("month"))
    day = int(fixture.group("day"))
    try:
        fixture_date = datetime(year, month, day).date()
    except ValueError:
        return result

    # Labels omit the year. A late-December fixture viewed in early January
    # belongs to the previous season-year; otherwise use the current year.
    if not fixture.group("year") and fixture_date - today > timedelta(days=180):
        fixture_date = fixture_date.replace(year=year - 1)

    hour_text, minute_text = fixture.group("clock").split(":", 1)
    local_start = datetime.combine(
        fixture_date,
        datetime_time(int(hour_text), int(minute_text)),
        tzinfo=LONDON,
    )
    result["title"] = f"{fixture.group('home').strip()} vs {fixture.group('away').strip()}"
    result["start_utc"] = local_start.astimezone(UTC)
    return result


def _parse_european_league_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse dynamic Bundesliga, La Liga, Serie A, and Ligue 1 labels."""
    channel_match = EUROPEAN_LEAGUE_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None

    league_key = re.sub(r"\s+", "", channel_match.group("league")).casefold()
    league = {
        "bundesliga": "Bundesliga",
        "laliga": "La Liga",
        "seriea": "Serie A",
        "ligue1": "Ligue 1",
    }[league_key]
    number = int(channel_match.group("number"))
    result: dict[str, Any] = {
        "channel_name": f"{league} {number:02d}",
        "league": league,
        "title": "",
        "start_utc": None,
    }
    details = channel_match.group("details").strip()
    if not details or details.casefold() in {"no event", "no match", "no game"}:
        return result

    fixture = EUROPEAN_LEAGUE_FIXTURE_PATTERN.match(details)
    if not fixture:
        return result

    matchup_text = LEAGUE_NAME_PREFIX_PATTERN.sub("", fixture.group("matchup").strip(), count=1)
    matchup = LEAGUE_MATCHUP_PATTERN.match(matchup_text)
    if not matchup:
        return result

    year = int(fixture.group("year") or today.year)
    try:
        local_date = datetime.strptime(
            f"{fixture.group('date')} {year}", "%d %b %Y"
        ).date()
    except ValueError:
        return result

    if not fixture.group("year"):
        day_delta = (local_date - today).days
        if day_delta > 180:
            local_date = local_date.replace(year=year - 1)
        elif day_delta < -180:
            local_date = local_date.replace(year=year + 1)

    clock = re.sub(r"\s+", "", fixture.group("clock")).upper()
    try:
        local_clock = datetime.strptime(clock, "%I:%M%p").time()
    except ValueError:
        return result

    local_start = datetime.combine(local_date, local_clock)
    zone = fixture.group("zone").upper()
    if zone in {"LONDON", "UK", "BST"}:
        local_start = local_start.replace(tzinfo=LONDON)
    else:
        local_start = local_start.replace(tzinfo=UTC)

    result["title"] = f"{matchup.group('home').strip()} vs {matchup.group('away').strip()}"
    result["start_utc"] = local_start.astimezone(UTC)
    return result


def _parse_mls_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse dynamic MLS fixture labels with Eastern kickoff times."""
    channel_match = MLS_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None

    number = int(channel_match.group("number"))
    result: dict[str, Any] = {
        "channel_name": f"MLS {number:02d}",
        "league": "MLS",
        "title": "",
        "start_utc": None,
    }
    details = channel_match.group("details").strip()
    if not details or details.casefold() in {"no event", "no match", "no game"}:
        return result

    fixture = MLS_FIXTURE_PATTERN.match(details)
    at_fixture = MLS_AT_FIXTURE_PATTERN.match(details) if not fixture else None
    if not fixture and not at_fixture:
        return result

    try:
        if fixture:
            year = int(fixture.group("year") or today.year)
            local_date = datetime.strptime(
                f"{fixture.group('date')}/{year}", "%m/%d/%Y"
            ).date()
            home, away = fixture.group("home"), fixture.group("away")
        else:
            year = int(at_fixture.group("year") or today.year)
            local_date = datetime.strptime(
                f"{at_fixture.group('date')} {year}", "%d %b %Y"
            ).date()
            matchup = LEAGUE_MATCHUP_PATTERN.match(at_fixture.group("matchup").strip())
            if not matchup:
                return result
            home, away = matchup.group("home"), matchup.group("away")
        clock_text = re.sub(r"\s+", "", (fixture or at_fixture).group("clock")).upper()
        local_clock = datetime.strptime(clock_text, "%I:%M%p").time()
    except ValueError:
        return result

    if not (fixture or at_fixture).group("year"):
        day_delta = (local_date - today).days
        if day_delta > 180:
            local_date = local_date.replace(year=year - 1)
        elif day_delta < -180:
            local_date = local_date.replace(year=year + 1)

    # Provider labels use Eastern Time (the date determines EST or EDT).
    local_start = datetime.combine(local_date, local_clock, tzinfo=EASTERN)
    result["title"] = f"{home.strip()} vs {away.strip()}"
    result["start_utc"] = local_start.astimezone(UTC)
    return result


def _normalize_ligue1_team_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    normalized = LIGUE1_TEAM_ALIASES.get(normalized, normalized)
    normalized = re.sub(r"\b(?:fc|afc|cf|sc)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _correct_ligue1_kickoff_times(
    targets: dict[str, dict[str, Any]],
) -> None:
    """Replace Ligue 1 feed timestamps with matched ESPN fixture kickoffs.

    The provider's Ligue 1 labels can show a stream/pregame start instead of
    the match kickoff. If the public scoreboard returns one unambiguous match
    for the same home/away teams and UK calendar date, use its UTC start time.
    Leave the provider time intact when lookup or matching is uncertain.
    """
    dates = sorted({
        item["start_utc"].astimezone(LONDON).date()
        for item in targets.values()
        if item.get("league") == "Ligue 1"
        and item.get("title")
        and item.get("start_utc") is not None
    })
    if not dates:
        return

    fixtures_by_date: dict[Any, list[dict[str, Any]]] = {}
    for fixture_date in dates:
        try:
            response = requests.get(
                ESPN_LIGUE1_SCOREBOARD_URL,
                params={"dates": fixture_date.strftime("%Y%m%d")},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"Ligue 1 kickoff lookup failed for {fixture_date}: {exc}")
            continue

        fixtures: list[dict[str, Any]] = []
        for event in payload.get("events", []):
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competition = competitions[0]
            competitors = competition.get("competitors") or []
            home = next(
                (item.get("team", {}).get("displayName", "")
                 for item in competitors if item.get("homeAway") == "home"),
                "",
            )
            away = next(
                (item.get("team", {}).get("displayName", "")
                 for item in competitors if item.get("homeAway") == "away"),
                "",
            )
            kickoff_text = competition.get("date") or event.get("date")
            if not home or not away or not kickoff_text:
                continue
            try:
                kickoff = datetime.fromisoformat(kickoff_text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if kickoff.tzinfo is None:
                continue
            if kickoff.astimezone(LONDON).date() != fixture_date:
                continue
            fixtures.append({"home": home, "away": away, "kickoff": kickoff.astimezone(UTC)})
        fixtures_by_date[fixture_date] = fixtures

    for item in targets.values():
        if (
            item.get("league") != "Ligue 1"
            or not item.get("title")
            or item.get("start_utc") is None
        ):
            continue
        fixture_date = item["start_utc"].astimezone(LONDON).date()
        teams = re.split(r"\s+vs\.?\s+", item["title"], maxsplit=1, flags=re.IGNORECASE)
        if len(teams) != 2:
            continue
        expected_home, expected_away = map(_normalize_ligue1_team_name, teams)
        matches = [
            fixture for fixture in fixtures_by_date.get(fixture_date, [])
            if _normalize_ligue1_team_name(fixture["home"]) == expected_home
            and _normalize_ligue1_team_name(fixture["away"]) == expected_away
        ]
        if len(matches) != 1:
            print(
                f"No unambiguous Ligue 1 kickoff match for {item['title']} "
                f"on {fixture_date}; keeping the provider time."
            )
            continue
        old_start = item["start_utc"]
        item["start_utc"] = matches[0]["kickoff"]
        if old_start != item["start_utc"]:
            print(
                f"Corrected Ligue 1 kickoff for {item['title']}: "
                f"{old_start.strftime('%H:%M GMT')} → "
                f"{item['start_utc'].strftime('%H:%M GMT')}."
            )


def _parse_dazn_dynamic_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse labels such as `DAZN UK 02: Event @ 25 Oct 07:00 AM London`."""
    channel_match = DAZN_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None

    number = int(channel_match.group("number"))
    result: dict[str, Any] = {
        "channel_name": f"DAZN UK {number:02d}",
        "title": "",
        "start_utc": None,
    }
    details = channel_match.group("details").strip()
    if not details or details.casefold() in {"no event", "no match", "no game"}:
        return result

    fixture = DAZN_FIXTURE_PATTERN.match(details)
    if not fixture:
        return result

    year = int(fixture.group("year") or today.year)
    try:
        event_date = datetime.strptime(
            f"{fixture.group('date')} {year}", "%d %b %Y"
        ).date()
    except ValueError:
        return result

    if not fixture.group("year"):
        day_delta = (event_date - today).days
        if day_delta > 180:
            event_date = event_date.replace(year=year - 1)
        elif day_delta < -180:
            event_date = event_date.replace(year=year + 1)

    clock = re.sub(r"\s+", "", fixture.group("clock")).upper()
    try:
        local_clock = datetime.strptime(clock, "%I:%M%p").time()
    except ValueError:
        return result

    local_start = datetime.combine(event_date, local_clock, tzinfo=LONDON)
    result["title"] = fixture.group("title").strip()
    result["start_utc"] = local_start.astimezone(UTC)
    return result


def _parse_paramount_dynamic_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse labels such as `Paramount+ 03: Event @ 24 Oct 07:20 AM ET`."""
    channel_match = PARAMOUNT_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None
    number = int(channel_match.group("number"))
    details = channel_match.group("details").strip()
    result: dict[str, Any] = {
        "channel_name": f"Paramount+ {number:02d}",
        "title": "",
        "start_utc": None,
    }
    fixture = PARAMOUNT_FIXTURE_PATTERN.match(details)
    if not fixture:
        return result

    year = int(fixture.group("year") or today.year)
    try:
        local_date = datetime.strptime(
            f"{fixture.group('date')} {year}", "%d %b %Y"
        ).date()
    except ValueError:
        return result

    if not fixture.group("year"):
        delta = (local_date - today).days
        if delta > 180:
            local_date = local_date.replace(year=year - 1)
        elif delta < -180:
            local_date = local_date.replace(year=year + 1)

    clock = re.sub(r"\s+", "", fixture.group("clock")).upper()
    try:
        local_time = datetime.strptime(
            f"{local_date.isoformat()} {clock}", "%Y-%m-%d %I:%M%p"
        )
    except ValueError:
        return result
    zone_name = fixture.group("zone").upper()
    candidates = [local_time.replace(tzinfo=EASTERN, fold=fold) for fold in (0, 1)]
    if zone_name in {"EDT", "EST"}:
        local_time = next((dt for dt in candidates if dt.tzname() == zone_name), None)
        if local_time is None:
            return result
    else:
        local_time = candidates[0]
    result["title"] = fixture.group("title").strip()
    result["start_utc"] = local_time.astimezone(UTC)
    return result


def _parse_additional_sports_event_label(
    label: str, today: Any, provider: str
) -> dict[str, Any] | None:
    """Parse Disney+, ESPN+, and MAX UK events with provider-local timestamps."""
    if provider == "Disney+ Events":
        channel_pattern = DISNEY_EVENT_CHANNEL_PATTERN
        maximum_number = 500
    elif provider == "ESPN+":
        channel_pattern = ESPN_EVENT_CHANNEL_PATTERN
        maximum_number = 400
    elif provider == "MAX UK":
        channel_pattern = MAX_UK_CHANNEL_PATTERN
        maximum_number = 100
    else:
        raise ValueError(f"Unsupported sports event provider: {provider}")

    channel_match = channel_pattern.match(label.strip())
    if not channel_match:
        return None
    number = int(channel_match.group("number"))
    if not 1 <= number <= maximum_number:
        return None

    result: dict[str, Any] = {
        "channel_name": f"{provider} {number:02d}",
        "title": "",
        "start_utc": None,
    }
    details = channel_match.group("details").strip()
    if not details or details.casefold() in {"no event", "no match", "no game"}:
        return result

    fixture = SPORTS_EVENT_FIXTURE_PATTERN.match(details)
    if not fixture:
        return result

    date_text = fixture.group("date")
    date_formats = ("%d %b %Y", "%b %d %Y")
    local_date = None
    year_text = fixture.groupdict().get("year")
    year = int(year_text or today.year)
    for date_format in date_formats:
        try:
            local_date = datetime.strptime(f"{date_text} {year}", date_format).date()
            break
        except ValueError:
            continue
    if local_date is None:
        return result

    if not year_text:
        day_delta = (local_date - today).days
        if day_delta > 180:
            local_date = local_date.replace(year=year - 1)
        elif day_delta < -180:
            local_date = local_date.replace(year=year + 1)

    clock = re.sub(r"\s+", "", fixture.group("clock")).upper()
    clock_format = "%I:%M%p" if ":" in clock else "%I%p"
    try:
        local_clock = datetime.strptime(clock, clock_format).time()
    except ValueError:
        return result

    local_time = datetime.combine(local_date, local_clock)
    zone_name = fixture.group("zone").upper()
    if provider == "MAX UK" or zone_name in {"LONDON", "UK", "BST", "GMT", "UTC"}:
        local_time = local_time.replace(tzinfo=LONDON)
    else:
        candidates = [local_time.replace(tzinfo=EASTERN, fold=fold) for fold in (0, 1)]
        if zone_name in {"EDT", "EST"}:
            local_time = next((dt for dt in candidates if dt.tzname() == zone_name), None)
            if local_time is None:
                return result
        else:
            local_time = candidates[0]

    result["title"] = fixture.group("title").strip()
    result["start_utc"] = local_time.astimezone(UTC)
    return result


def _parse_model_response(response: Any) -> list[dict[str, str]]:
    """Parse the model's requested Python-list format, tolerating code fences."""
    text = response if isinstance(response, str) else (getattr(response, "text", None) or "")
    if not text:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = getattr(getattr(candidates[0], "content", None), "parts", []) or []
            text = "".join(getattr(part, "text", "") or "" for part in parts)

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Backward compatibility with the prompt's Python-list response format.
        result = ast.literal_eval(text)
    if not isinstance(result, list):
        raise ValueError("Model response was not a list")
    for item in result:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str)
            for key in ("raw", "clean_title", "description")
        ):
            raise ValueError("Model returned an item with missing or invalid fields")
    return result


def _groq_generate_text(prompt: str, api_key: str) -> str:
    """Generate a web-grounded response with Groq's built-in browser search."""
    response = requests.post(
        GROQ_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_completion_tokens": 4096,
            "tool_choice": "required",
            "tools": [{"type": "browser_search"}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not response.ok:
        # Include the provider's JSON error: the HTTP status alone doesn't
        # distinguish request-rate, token, daily-quota, or account limits.
        error_detail = response.text.strip()[:2000]
        raise requests.HTTPError(
            f"Groq API returned HTTP {response.status_code}: {error_detail}",
            response=response,
        )
    try:
        return response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError("Groq returned no text content") from exc


def _parse_json_array(text: str) -> list[Any]:
    """Parse a JSON array, allowing a surrounding Markdown code fence."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text[:-3]
    parsed = json.loads(text.strip())
    if not isinstance(parsed, list):
        raise ValueError("Model response was not a JSON array")
    return parsed


def ai_clean_sport_titles(
    channel_names: list[str], client: genai.Client | None, groq_api_key: str | None = None
) -> dict[str, dict[str, str]]:
    """Ask Gemini to clean a batch of mirrored event titles."""
    if not channel_names:
        return {}

    prompt = """You organize sports event titles for an IPTV electronic program guide.
For every input string, use Google Search to find reliable information about
the specific event when possible. Return its exact original value in `raw`, a
concise properly formatted `clean_title`, and a brief factual `description`
based on the search results. Do not invent teams, competitions, or event
details. If reliable information is unavailable, say only what is known from
the title. Return only a JSON array of objects with exactly these string fields:
raw, clean_title, description.

Input titles (JSON):
""" + json.dumps(channel_names, ensure_ascii=False)

    if client is None:
        if not groq_api_key:
            return {}
        try:
            parsed = _parse_model_response(_groq_generate_text(prompt, groq_api_key))
            expected = set(channel_names)
            mapping = {item["raw"]: item for item in parsed if item["raw"] in expected}
            if expected - mapping.keys():
                raise ValueError(f"Groq omitted {len(expected - mapping.keys())} requested title(s)")
            return mapping
        except Exception as exc:
            print(f"Groq LIVE EVENT title cleanup failed: {exc}")
            return {}

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            parsed = _parse_model_response(response)
            expected = set(channel_names)
            mapping = {}
            for item in parsed:
                raw = item["raw"]
                if raw in expected:
                    mapping[raw] = item
            missing = expected - mapping.keys()
            if missing:
                raise ValueError(f"Gemini omitted {len(missing)} requested title(s)")
            return mapping
        except Exception as exc:
            print(f"Gemini attempt {attempt + 1}/{MAX_RETRIES} failed: {exc}")
            if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc):
                print("Gemini quota is exhausted; trying Groq for this title batch.")
                break
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
    if groq_api_key:
        try:
            parsed = _parse_model_response(_groq_generate_text(prompt, groq_api_key))
            expected = set(channel_names)
            mapping = {item["raw"]: item for item in parsed if item["raw"] in expected}
            if expected - mapping.keys():
                raise ValueError(f"Groq omitted {len(expected - mapping.keys())} requested title(s)")
            return mapping
        except Exception as exc:
            print(f"Groq LIVE EVENT title cleanup failed: {exc}")
    print("Skipping this batch after repeated failures.")
    return {}


def ai_describe_events(
    events: list[dict[str, str]], client: genai.Client | None, groq_api_key: str | None = None
) -> dict[str, str]:
    """Create short, web-grounded EPG descriptions keyed to event instances."""
    descriptions: dict[str, str] = {}
    if not events:
        return descriptions

    gemini_exhausted = client is None
    for start in range(0, len(events), DESCRIPTION_BATCH_SIZE):
        batch = events[start : start + DESCRIPTION_BATCH_SIZE]
        prompt = """For each sports event below, use Google Search to find reliable,
current information about that exact event. Write a concise EPG description
(one or two sentences) identifying the competition and relevant event details.
Do not guess or add unsupported facts. If search results do not identify the
event reliably, use only the supplied title and context. Return only a JSON
array with one object per input, with exactly these string fields: key,
description. Preserve each key exactly.

Events (JSON):
""" + json.dumps(batch, ensure_ascii=False)

        try:
            if gemini_exhausted:
                raise RuntimeError("Gemini unavailable; use configured fallback")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            text = (getattr(response, "text", None) or "").strip()
            if not text:
                raise ValueError("Gemini returned an empty description response")
            parsed = _parse_json_array(text)
            expected = {event["key"] for event in batch}
            for item in parsed:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("key"), str)
                    and isinstance(item.get("description"), str)
                    and item["key"] in expected
                ):
                    descriptions[item["key"]] = item["description"].strip()
            missing = expected - descriptions.keys()
            if missing:
                print(f"Gemini omitted {len(missing)} event description(s) in this batch.")
        except Exception as exc:
            if not gemini_exhausted:
                print(f"Gemini event-description batch failed: {exc}")
            if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc) or client is None:
                gemini_exhausted = True
                print("Gemini is unavailable; trying Groq for this and remaining description batches.")
            if groq_api_key:
                try:
                    groq_text = _groq_generate_text(prompt, groq_api_key)
                    parsed = _parse_json_array(groq_text)
                    expected = {event["key"] for event in batch}
                    for item in parsed:
                        if (
                            isinstance(item, dict)
                            and isinstance(item.get("key"), str)
                            and isinstance(item.get("description"), str)
                            and item["key"] in expected
                        ):
                            descriptions[item["key"]] = item["description"].strip()
                    missing = expected - descriptions.keys()
                    if missing:
                        print(f"Groq omitted {len(missing)} event description(s) in this batch.")
                except Exception as groq_exc:
                    print(f"Groq event-description batch failed: {groq_exc}")
            # Keep the schedule publishing if either model fails.
    return descriptions


def _make_ai_cache_key(kind: str, payload: dict[str, str]) -> str:
    material = json.dumps(
        {"version": AI_CACHE_VERSION, "model": GEMINI_MODEL, "kind": kind, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_ai_cache() -> dict[str, Any]:
    try:
        cached = json.loads(AI_CACHE_FILE.read_text(encoding="utf-8"))
        if cached.get("version") == AI_CACHE_VERSION and isinstance(cached.get("entries"), dict):
            _prune_ai_cache(cached)
            return cached
        print("AI cache version is outdated or invalid; starting a fresh cache.")
    except FileNotFoundError:
        print("No saved AI cache yet; the first run will populate it.")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        print(f"Could not read AI cache ({exc}); starting a fresh cache.")
    return {"version": AI_CACHE_VERSION, "entries": {}}


def _prune_ai_cache(cache: dict[str, Any]) -> None:
    """Drop expired failures and successful results older than 30 days."""
    entries = cache["entries"]
    now = datetime.now(UTC)
    cutoff = now - AI_CACHE_MAX_AGE
    stale_keys: list[str] = []

    for key, entry in entries.items():
        if not isinstance(entry, dict):
            stale_keys.append(key)
            continue

        timestamp_value = entry.get("retry_after") if entry.get("failed") else entry.get("created_at")
        if not isinstance(timestamp_value, str):
            continue
        try:
            timestamp = datetime.fromisoformat(timestamp_value)
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        if entry.get("failed"):
            if timestamp <= now:
                stale_keys.append(key)
        elif timestamp < cutoff:
            stale_keys.append(key)

    for key in stale_keys:
        del entries[key]
    if stale_keys:
        print(f"Pruned {len(stale_keys)} expired or stale AI cache entr(y/ies).")


def _save_ai_cache(cache: dict[str, Any]) -> None:
    temporary = AI_CACHE_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(AI_CACHE_FILE)


def _cache_failure_is_active(entry: Any) -> bool:
    if not isinstance(entry, dict) or not entry.get("failed"):
        return False
    try:
        retry_after = datetime.fromisoformat(entry["retry_after"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(UTC) < retry_after


def process_epg() -> None:
    client: genai.Client | None = None
    groq_api_key = os.environ.get("GROQ_API_KEY")
    ai_cache = _load_ai_cache()
    ai_cache_entries: dict[str, Any] = ai_cache["entries"]

    def get_client() -> genai.Client | None:
        nonlocal client
        if client is None:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                print("GEMINI_API_KEY is unavailable; trying Groq if configured.")
                return None
            client = genai.Client(api_key=api_key)
        return client

    print("Downloading EPG XML...")
    response = requests.get(EPG_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    # XMLTV documents may declare a non-UTF-8 encoding; parse the response bytes.
    root = ET.fromstring(response.content)

    print("Finding dynamic live-event and football channels...")
    targets: dict[str, dict[str, Any]] = {}
    for channel in root.iter("channel"):
        ch_id = channel.get("id")
        if not ch_id:
            continue
        display_names = [(el.text or "").strip() for el in channel.findall("display-name")]
        if not display_names:
            continue
        for display_name in display_names:
            parsed_name = _event_number_and_suffix(display_name)
            if parsed_name:
                core, suffix = parsed_name
                permanent_title = "UFC Special Feed" if core == "LIVE EVENT 01" else ""
                no_event = _is_no_event_title(suffix)
                event_title = permanent_title or next(
                    (name for name in display_names
                     if not _event_number_and_suffix(name)
                     and not _is_no_event_title(name)),
                    "",
                )
                if not event_title and suffix and not no_event:
                    event_title = _format_title_event_name(suffix)
                if event_title and not permanent_title:
                    event_title = _format_title_event_name(event_title)
                    no_event = _is_no_event_title(event_title)
                    if no_event:
                        event_title = ""
                times = _parse_eastern_starts(suffix)
                targets[ch_id] = {
                    "core": core,
                    "event_title": event_title,
                    "start_times": times,
                    "permanent": bool(permanent_title),
                    "no_event": no_event or not event_title,
                }
                # Use the first matching marker alias; channel names are
                # normalized below regardless of their daily suffix.
                break

    epl_today = datetime.now(LONDON).date()
    mls_today = datetime.now(EASTERN).date()
    max_uk_today = datetime.now(LONDON).date()
    epl_targets: dict[str, dict[str, Any]] = {}
    european_league_targets: dict[str, dict[str, Any]] = {}
    dazn_targets: dict[str, dict[str, Any]] = {}
    dazn_today = datetime.now(LONDON).date()
    paramount_today = datetime.now(EASTERN).date()
    paramount_targets: dict[str, dict[str, Any]] = {}
    disney_event_targets: dict[str, dict[str, Any]] = {}
    espn_event_targets: dict[str, dict[str, Any]] = {}
    max_uk_targets: dict[str, dict[str, Any]] = {}
    for channel in root.iter("channel"):
        ch_id = channel.get("id")
        if not ch_id:
            continue
        for display_el in channel.findall("display-name"):
            parsed = _parse_epl_dynamic_label((display_el.text or "").strip(), epl_today)
            if parsed:
                epl_targets[ch_id] = parsed
                continue
            parsed = _parse_european_league_label(
                (display_el.text or "").strip(), epl_today
            )
            if parsed:
                european_league_targets[ch_id] = parsed
                continue
            parsed = _parse_mls_label((display_el.text or "").strip(), mls_today)
            if parsed:
                european_league_targets[ch_id] = parsed
                continue
            parsed = _parse_dazn_dynamic_label(
                (display_el.text or "").strip(), dazn_today
            )
            if parsed:
                dazn_targets[ch_id] = parsed
                continue
            parsed = _parse_paramount_dynamic_label(
                (display_el.text or "").strip(), paramount_today
            )
            if parsed:
                paramount_targets[ch_id] = parsed
                break
            parsed = _parse_additional_sports_event_label(
                (display_el.text or "").strip(), paramount_today, "Disney+ Events"
            )
            if parsed:
                disney_event_targets[ch_id] = parsed
                break
            parsed = _parse_additional_sports_event_label(
                (display_el.text or "").strip(), paramount_today, "ESPN+"
            )
            if parsed:
                espn_event_targets[ch_id] = parsed
                break
            parsed = _parse_additional_sports_event_label(
                (display_el.text or "").strip(), max_uk_today, "MAX UK"
            )
            if parsed:
                max_uk_targets[ch_id] = parsed
                break

    # Some providers expose the event title only in the programme entries.
    for programme in root.iter("programme"):
        ch_id = programme.get("channel")
        if ch_id in targets and not targets[ch_id]["event_title"]:
            title = (programme.findtext("title") or "").strip()
            clean_title = _format_title_event_name(title)
            if _is_no_event_title(clean_title):
                targets[ch_id]["no_event"] = True
            elif clean_title:
                targets[ch_id]["event_title"] = clean_title
                targets[ch_id]["no_event"] = False
        if ch_id and ch_id not in epl_targets:
            parsed = _parse_epl_dynamic_label(
                (programme.findtext("title") or "").strip(), epl_today
            )
            if parsed:
                epl_targets[ch_id] = parsed
        if ch_id and ch_id not in european_league_targets:
            parsed = _parse_european_league_label(
                (programme.findtext("title") or "").strip(), epl_today
            )
            if parsed:
                european_league_targets[ch_id] = parsed
        if ch_id and ch_id not in european_league_targets:
            parsed = _parse_mls_label(
                (programme.findtext("title") or "").strip(), mls_today
            )
            if parsed:
                european_league_targets[ch_id] = parsed
        if ch_id and ch_id not in dazn_targets:
            parsed = _parse_dazn_dynamic_label(
                (programme.findtext("title") or "").strip(), dazn_today
            )
            if parsed:
                dazn_targets[ch_id] = parsed
        if ch_id and ch_id not in paramount_targets:
            parsed = _parse_paramount_dynamic_label(
                (programme.findtext("title") or "").strip(), paramount_today
            )
            if parsed:
                paramount_targets[ch_id] = parsed
        if ch_id and ch_id not in disney_event_targets:
            parsed = _parse_additional_sports_event_label(
                (programme.findtext("title") or "").strip(),
                paramount_today,
                "Disney+ Events",
            )
            if parsed:
                disney_event_targets[ch_id] = parsed
        if ch_id and ch_id not in espn_event_targets:
            parsed = _parse_additional_sports_event_label(
                (programme.findtext("title") or "").strip(),
                paramount_today,
                "ESPN+",
            )
            if parsed:
                espn_event_targets[ch_id] = parsed
        if ch_id and ch_id not in max_uk_targets:
            parsed = _parse_additional_sports_event_label(
                (programme.findtext("title") or "").strip(),
                max_uk_today,
                "MAX UK",
            )
            if parsed:
                max_uk_targets[ch_id] = parsed

    _correct_ligue1_kickoff_times(european_league_targets)

    # Collect distinct fixture events for grounded descriptions. Static EPL/EFL
    # team channels carry their next fixture in the programme description.
    current_utc_date = datetime.now(UTC).date()
    description_events: dict[str, dict[str, str]] = {}
    epl_description_keys: dict[str, str] = {}
    european_league_description_keys: dict[str, str] = {}
    dazn_description_keys: dict[str, str] = {}
    paramount_description_keys: dict[str, str] = {}
    disney_event_description_keys: dict[str, str] = {}
    espn_event_description_keys: dict[str, str] = {}
    max_uk_description_keys: dict[str, str] = {}
    football_programme_description_keys: dict[int, str] = {}

    def add_description_event(key: str, title: str, context: str) -> None:
        if title:
            description_events.setdefault(
                key, {"key": key, "title": title, "context": context}
            )

    for ch_id, item in epl_targets.items():
        if (
            item["title"]
            and item["start_utc"]
            and item["start_utc"].date() >= current_utc_date
        ):
            start_utc = item["start_utc"]
            key = f"epl-dynamic:{start_utc:%Y%m%d%H%M}:{item['title']}"
            epl_description_keys[ch_id] = key
            add_description_event(
                key,
                item["title"],
                f"{item['league']} fixture scheduled {start_utc.strftime('%d/%m/%Y %H:%M GMT')}",
            )

    for ch_id, item in european_league_targets.items():
        if (
            item["title"]
            and item["start_utc"]
            and item["start_utc"].date() >= current_utc_date
        ):
            start_utc = item["start_utc"]
            key = f"{item['league'].casefold()}:{start_utc:%Y%m%d%H%M}:{item['title']}"
            european_league_description_keys[ch_id] = key
            add_description_event(
                key,
                item["title"],
                f"{item['league']} fixture scheduled {start_utc.strftime('%d/%m/%Y %H:%M GMT')}",
            )

    for ch_id, item in dazn_targets.items():
        if (
            item["title"]
            and item["start_utc"]
            and item["start_utc"].date() >= current_utc_date
        ):
            start_utc = item["start_utc"]
            key = f"dazn:{start_utc:%Y%m%d%H%M}:{item['title']}"
            dazn_description_keys[ch_id] = key
            add_description_event(
                key,
                item["title"],
                f"DAZN UK event scheduled {start_utc.strftime('%d/%m/%Y %H:%M GMT')}",
            )

    for ch_id, item in paramount_targets.items():
        if (
            item["title"]
            and item["start_utc"]
            and item["start_utc"].date() >= current_utc_date
        ):
            start_utc = item["start_utc"]
            key = f"paramount:{start_utc:%Y%m%d%H%M}:{item['title']}"
            paramount_description_keys[ch_id] = key
            add_description_event(
                key,
                item["title"],
                f"Paramount+ event scheduled {start_utc.strftime('%d/%m/%Y %H:%M GMT')}",
            )

    for provider, targets_for_provider, key_map in (
        ("Disney+ Events", disney_event_targets, disney_event_description_keys),
        ("ESPN+", espn_event_targets, espn_event_description_keys),
        ("MAX UK", max_uk_targets, max_uk_description_keys),
    ):
        for ch_id, item in targets_for_provider.items():
            if (
                item["title"]
                and item["start_utc"]
                and item["start_utc"].date() >= current_utc_date
            ):
                start_utc = item["start_utc"]
                key = f"sports-event:{start_utc:%Y%m%d%H%M}:{item['title']}"
                key_map[ch_id] = key
                add_description_event(
                    key,
                    item["title"],
                    f"{provider} event scheduled {start_utc.strftime('%d/%m/%Y %H:%M GMT')}",
                )

    football_channel_ids = {
        channel.get("id")
        for channel in root.iter("channel")
        if (
            (channel.get("id") or "").upper().startswith(("EPL", "EFL"))
            or "spfl" in (channel.get("id") or "").casefold()
            or channel.get("id") in european_league_targets
        )
    }
    for programme in root.iter("programme"):
        ch_id = programme.get("channel")
        if ch_id not in football_channel_ids:
            continue
        desc = programme.findtext("desc") or ""
        desc = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            desc,
        )
        fixture_match = NEXT_GAME_PATTERN.search(desc)
        if not fixture_match:
            continue
        formatted = _format_next_game(fixture_match)
        if not formatted:
            continue
        event_title = f"{fixture_match.group('home').strip()} vs {fixture_match.group('away').strip()}"
        key = f"team-fixture:{formatted}"
        football_programme_description_keys[id(programme)] = key
        add_description_event(key, event_title, f"{ch_id}; {formatted}")

    event_date = datetime.now(EASTERN).date()
    dazn_event_titles = {
        item["title"] for item in dazn_targets.values()
        if (
            item["title"]
            and item["start_utc"]
            and item["start_utc"].date() >= current_utc_date
        )
    }
    active_titles = sorted({
        item["event_title"] for item in targets.values()
        if (
            item["event_title"]
            and item["start_times"]
            and not item["permanent"]
            and not item["no_event"]
        )
    } | dazn_event_titles)
    print(
        f"Found {len(targets)} LIVE EVENT channel(s), {len(dazn_targets)} DAZN channel(s), "
        f"{len(disney_event_targets)} Disney+ Events channel(s), "
        f"{len(espn_event_targets)} ESPN+ channel(s), "
        f"{len(max_uk_targets)} MAX UK channel(s), "
        f"{len(epl_targets) + len(european_league_targets)} dynamic football channel(s), "
        f"and {len(active_titles)} Event Changes title(s) with parseable start times."
    )

    cleaned_mapping: dict[str, dict[str, str]] = {}
    live_cache_keys = {
        title: _make_ai_cache_key(
            "live-title",
            {"title": title, "event_date": event_date.isoformat()},
        )
        for title in active_titles
    }
    titles_to_clean: list[str] = []
    cached_failures = 0
    for title, cache_key in live_cache_keys.items():
        cache_entry = ai_cache_entries.get(cache_key, {})
        result = cache_entry.get("result") if isinstance(cache_entry, dict) else None
        if (
            isinstance(result, dict)
            and result.get("raw") == title
            and isinstance(result.get("clean_title"), str)
            and isinstance(result.get("description"), str)
        ):
            cleaned_mapping[title] = result
        elif _cache_failure_is_active(cache_entry):
            if groq_api_key:
                titles_to_clean.append(title)
            else:
                cached_failures += 1
        else:
            titles_to_clean.append(title)
    print(
        f"Reusing {len(cleaned_mapping)} cached LIVE EVENT title result(s); "
        f"{cached_failures} recent failure(s) held for cooldown; "
        f"{len(titles_to_clean)} need AI cleanup."
    )
    if titles_to_clean:
        gemini_client = get_client()
        if gemini_client or groq_api_key:
            for start in range(0, len(titles_to_clean), BATCH_SIZE):
                batch = titles_to_clean[start : start + BATCH_SIZE]
                print(f"Cleaning batch {start // BATCH_SIZE + 1} ({len(batch)} titles)...")
                results = ai_clean_sport_titles(batch, gemini_client, groq_api_key)
                for result in results.values():
                    result["clean_title"] = _format_title_event_name(result["clean_title"])
                cleaned_mapping.update(results)
                for title, result in results.items():
                    ai_cache_entries[live_cache_keys[title]] = {
                        "kind": "live-title",
                        "created_at": datetime.now(UTC).isoformat(),
                        "result": result,
                    }
                failed_titles = set(batch) - results.keys()
                retry_after = (datetime.now(UTC) + CACHE_FAILURE_COOLDOWN).isoformat()
                for title in failed_titles:
                    ai_cache_entries[live_cache_keys[title]] = {
                        "kind": "live-title",
                        "failed": True,
                        "retry_after": retry_after,
                    }

    event_descriptions: dict[str, str] = {}
    description_event_list = list(description_events.values())
    description_cache_keys: dict[str, str] = {}
    events_to_describe: list[dict[str, str]] = []
    cached_description_failures = 0
    for event in description_event_list:
        cache_key = _make_ai_cache_key(
            "event-description",
            {"key": event["key"], "title": event["title"], "context": event["context"]},
        )
        description_cache_keys[event["key"]] = cache_key
        cache_entry = ai_cache_entries.get(cache_key, {})
        result = cache_entry.get("result") if isinstance(cache_entry, dict) else None
        if isinstance(result, str) and result.strip():
            event_descriptions[event["key"]] = result
        elif _cache_failure_is_active(cache_entry):
            if groq_api_key:
                events_to_describe.append(event)
            else:
                cached_description_failures += 1
        else:
            events_to_describe.append(event)
    print(
        f"Reusing {len(event_descriptions)} cached event description(s); "
        f"{cached_description_failures} recent failure(s) held for cooldown; "
        f"{len(events_to_describe)} need AI descriptions."
    )
    if events_to_describe:
        gemini_client = get_client()
        if gemini_client or groq_api_key:
            new_descriptions = ai_describe_events(events_to_describe, gemini_client, groq_api_key)
            event_descriptions.update(new_descriptions)
            for event in events_to_describe:
                description = new_descriptions.get(event["key"])
                if description:
                    ai_cache_entries[description_cache_keys[event["key"]]] = {
                        "kind": "event-description",
                        "created_at": datetime.now(UTC).isoformat(),
                        "result": description,
                    }
                else:
                    ai_cache_entries[description_cache_keys[event["key"]]] = {
                        "kind": "event-description",
                        "failed": True,
                        "retry_after": (
                            datetime.now(UTC) + CACHE_FAILURE_COOLDOWN
                        ).isoformat(),
                    }
    print(
        f"Generated web-grounded descriptions for {len(event_descriptions)} "
        f"of {len(description_events)} distinct fixtures."
    )

    # Strip all changing event details from display names. Repeated aliases are
    # retained to preserve the XML structure, but contain only the stable label.
    for channel in root.iter("channel"):
        item = targets.get(channel.get("id"))
        if item:
            for display_el in channel.findall("display-name"):
                display_el.text = item["core"]
        item = dazn_targets.get(channel.get("id"))
        if item:
            for display_el in channel.findall("display-name"):
                display_el.text = item["channel_name"]

    # Replace provider placeholder rows on these channels with generated listings.
    generated: list[ET.Element] = []
    now_utc = datetime.now(UTC)

    def add_all_day_notice(channel_id: str, title: str) -> None:
        day_start = datetime.combine(now_utc.date(), datetime_time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        notice = ET.Element("programme", {
            "start": day_start.strftime("%Y%m%d%H%M%S %z"),
            "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
            "channel": channel_id,
        })
        ET.SubElement(notice, "title").text = title
        ET.SubElement(notice, "desc").text = title
        generated.append(notice)

    target_ids = set(targets)
    programmes_to_remove = [
        programme
        for programme in root.iter("programme")
        if programme.get("channel") in target_ids
    ]
    for programme in programmes_to_remove:
        root.remove(programme)

    for ch_id, item in targets.items():
        raw_title = item["event_title"]
        if item["permanent"]:
            start_utc = datetime.combine(datetime.now(UTC).date(), datetime_time.min, tzinfo=UTC)
            end_utc = start_utc + timedelta(days=1)
            programme = ET.Element("programme", {
                "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "stop": end_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(programme, "title").text = "UFC Special Feed"
            ET.SubElement(programme, "desc").text = "UFC Special Feed"
            generated.append(programme)
            continue
        if item["no_event"] or not raw_title:
            add_all_day_notice(ch_id, "No event today")
            continue
        result = cleaned_mapping.get(raw_title, {
            "clean_title": raw_title,
            "description": "Live event",
        })
        result["clean_title"] = _format_title_event_name(result["clean_title"])
        if not item["start_times"]:
            day_start = datetime.combine(now_utc.date(), datetime_time.min, tzinfo=UTC)
            day_end = day_start + timedelta(days=1)
            programme = ET.Element("programme", {
                "start": day_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(programme, "title").text = result["clean_title"]
            ET.SubElement(programme, "desc").text = result["description"]
            generated.append(programme)
            continue
        channel_generated_start = len(generated)
        scheduled_starts: list[datetime] = []
        for event_time in item["start_times"]:
            start_eastern = datetime.combine(event_date, event_time, tzinfo=EASTERN)
            start_utc = start_eastern.astimezone(UTC)
            scheduled_starts.append(start_utc)
            end_utc = datetime.combine(
                start_utc.date() + timedelta(days=1), datetime_time.min, tzinfo=UTC
            )
            programme = ET.Element("programme", {
                "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "stop": end_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(programme, "title").text = result["clean_title"]
            ET.SubElement(programme, "desc").text = result["description"]
            generated.append(programme)

        # Fill the gap before kickoff so UHF has an upcoming listing instead of
        # showing "No information". The pre-event slot ends exactly at kickoff.
        first_start = min(scheduled_starts)
        upcoming_start = datetime.combine(first_start.date(), datetime_time.min, tzinfo=UTC)
        upcoming_title = (
            f"Upcoming Event: {result['clean_title']} at {_format_gmt_time(first_start)}"
        )
        if upcoming_start < first_start and datetime.now(UTC) < first_start:
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": first_start.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = (
                f"{upcoming_title}\n\n{result['description']}"
            )
            generated.insert(channel_generated_start, upcoming)

    # Normalize DAZN UK 01–100 names and replace their provider placeholders.
    dazn_replace_ids = {
        ch_id for ch_id, item in dazn_targets.items()
        if item["title"] and item["start_utc"] is not None
    }
    programmes_to_remove = [
        programme
        for programme in root.iter("programme")
        if programme.get("channel") in dazn_replace_ids
    ]
    for programme in programmes_to_remove:
        root.remove(programme)

    for ch_id, item in dazn_targets.items():
        raw_title = item["title"]
        start_utc = item["start_utc"]
        if not raw_title or start_utc is None:
            print(f"No scheduled listing for {item['channel_name']} (no event or unparsed label).")
            continue
        if start_utc.date() < now_utc.date():
            add_all_day_notice(ch_id, "No event today")
            continue

        result = cleaned_mapping.get(raw_title, {
            "clean_title": raw_title,
            "description": "Live event",
        })
        clean_title = _format_title_event_name(result["clean_title"])
        description_key = dazn_description_keys.get(ch_id, "")
        description = event_descriptions.get(description_key, result["description"])
        day_start = datetime.combine(start_utc.date(), datetime_time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)

        upcoming_start = datetime.combine(now_utc.date(), datetime_time.min, tzinfo=UTC)
        if upcoming_start < start_utc and now_utc < start_utc:
            upcoming_title = (
                f"Upcoming Event: {clean_title} at {_format_gmt_time(start_utc)}"
            )
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = f"{upcoming_title}\n\n{description}"
            generated.append(upcoming)

        event = ET.Element("programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
            "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
            "channel": ch_id,
        })
        started_title = f"{clean_title} (Started {_format_gmt_time(start_utc)})"
        ET.SubElement(event, "title").text = started_title
        ET.SubElement(event, "desc").text = description
        generated.append(event)

    # Replace repeating source placeholders on the dynamic football event channels.
    dynamic_football_targets = {**epl_targets, **european_league_targets}
    programmes_to_remove = [
        programme
        for programme in root.iter("programme")
        if programme.get("channel") in dynamic_football_targets
    ]
    for programme in programmes_to_remove:
        root.remove(programme)

    for ch_id, item in dynamic_football_targets.items():
        title = item["title"]
        start_utc = item["start_utc"]
        if not title or start_utc is None:
            print(
                f"No fixture found for {item['channel_name']}; adding an all-day placeholder."
            )
            day_start = datetime.combine(epl_today, datetime_time.min, tzinfo=UTC)
            day_end = day_start + timedelta(days=1)
            no_match = ET.Element("programme", {
                "start": day_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(no_match, "title").text = "No match today"
            ET.SubElement(no_match, "desc").text = "No match today"
            generated.append(no_match)
            continue
        if start_utc.date() < now_utc.date():
            add_all_day_notice(ch_id, "No match today")
            continue
        day_start = datetime.combine(start_utc.date(), datetime_time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        event_end = min(start_utc + EPL_EVENT_DURATION, day_end)
        upcoming_start = day_start
        upcoming_title = f"Upcoming Match: {title} at {_format_gmt_time(start_utc)}"
        description_key = epl_description_keys.get(ch_id) or european_league_description_keys.get(ch_id, "")
        description = event_descriptions.get(description_key, title)
        if upcoming_start < start_utc and now_utc < start_utc:
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = (
                f"{upcoming_title}\n\n{description}"
            )
            generated.append(upcoming)
        fixture_programme = ET.Element("programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
            "stop": event_end.strftime("%Y%m%d%H%M%S %z"),
            "channel": ch_id,
        })
        ET.SubElement(fixture_programme, "title").text = title
        ET.SubElement(fixture_programme, "desc").text = description
        generated.append(fixture_programme)

        # Keep the channel populated after the 2.5-hour event, but only
        # through midnight on the fixture's GMT date. No next-day placeholder
        # is generated; the next daily source import will provide that day's data.
        if event_end < day_end:
            ended = ET.Element("programme", {
                "start": event_end.strftime("%Y%m%d%H%M%S %z"),
                "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(ended, "title").text = "Match ended"
            ET.SubElement(ended, "desc").text = "Match ended"
            generated.append(ended)

    # Normalize Paramount+ 01–100 names and replace their repeated placeholders.
    for channel in root.iter("channel"):
        item = paramount_targets.get(channel.get("id"))
        if item:
            for display_el in channel.findall("display-name"):
                display_el.text = item["channel_name"]
    paramount_replace_ids = {
        ch_id for ch_id, item in paramount_targets.items()
        if item["title"] and item["start_utc"] is not None
    }
    programmes_to_remove = [
        programme
        for programme in root.iter("programme")
        if programme.get("channel") in paramount_replace_ids
    ]
    for programme in programmes_to_remove:
        root.remove(programme)

    for ch_id, item in paramount_targets.items():
        title = item["title"]
        start_utc = item["start_utc"]
        if not title or start_utc is None:
            print(f"No fixture listing for {item['channel_name']} (empty or unparsed label).")
            continue
        if start_utc.date() < now_utc.date():
            add_all_day_notice(ch_id, "No event today")
            continue
        end_utc = datetime.combine(
            start_utc.date() + timedelta(days=1), datetime_time.min, tzinfo=UTC
        )
        upcoming_start = datetime.combine(start_utc.date(), datetime_time.min, tzinfo=UTC)
        upcoming_title = f"Upcoming Event: {title} at {_format_gmt_time(start_utc)}"
        description = event_descriptions.get(
            paramount_description_keys.get(ch_id, ""), title
        )
        if upcoming_start < start_utc and now_utc < start_utc:
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = (
                f"{upcoming_title}\n\n{description}"
            )
            generated.append(upcoming)
        fixture_programme = ET.Element("programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
            "stop": end_utc.strftime("%Y%m%d%H%M%S %z"),
            "channel": ch_id,
        })
        ET.SubElement(fixture_programme, "title").text = title
        ET.SubElement(fixture_programme, "desc").text = description
        generated.append(fixture_programme)

    sports_event_targets = {
        **disney_event_targets,
        **espn_event_targets,
        **max_uk_targets,
    }
    sports_event_description_keys = {
        **disney_event_description_keys,
        **espn_event_description_keys,
        **max_uk_description_keys,
    }
    for channel in root.iter("channel"):
        item = sports_event_targets.get(channel.get("id"))
        if item:
            for display_el in channel.findall("display-name"):
                display_el.text = item["channel_name"]
    sports_event_replace_ids = {
        ch_id for ch_id, item in sports_event_targets.items()
        if item["title"] and item["start_utc"] is not None
    }
    programmes_to_remove = [
        programme
        for programme in root.iter("programme")
        if programme.get("channel") in sports_event_replace_ids
    ]
    for programme in programmes_to_remove:
        root.remove(programme)

    for ch_id, item in sports_event_targets.items():
        title = item["title"]
        start_utc = item["start_utc"]
        if not title or start_utc is None:
            print(
                f"No fixture listing for {item['channel_name']} "
                "(no event or unparsed label)."
            )
            continue
        if start_utc.date() < now_utc.date():
            add_all_day_notice(ch_id, "No event today")
            continue

        day_start = datetime.combine(start_utc.date(), datetime_time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        description = event_descriptions.get(
            sports_event_description_keys.get(ch_id, ""), title
        )
        upcoming_title = f"Upcoming Event: {title} at {_format_gmt_time(start_utc)}"
        if day_start < start_utc and now_utc < start_utc:
            upcoming = ET.Element("programme", {
                "start": day_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = (
                f"{upcoming_title}\n\n{description}"
            )
            generated.append(upcoming)

        event = ET.Element("programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
            "stop": day_end.strftime("%Y%m%d%H%M%S %z"),
            "channel": ch_id,
        })
        ET.SubElement(event, "title").text = title
        ET.SubElement(event, "desc").text = description
        generated.append(event)

    first_programme_index = next(
        (index for index, element in enumerate(root) if element.tag == "programme"),
        len(root),
    )
    for offset, programme in enumerate(generated):
        root.insert(first_programme_index + offset, programme)

    football_channel_ids = {
        channel.get("id")
        for channel in root.iter("channel")
        if (
            (channel.get("id") or "").upper().startswith(("EPL", "EFL"))
            or "spfl" in (channel.get("id") or "").casefold()
            or channel.get("id") in european_league_targets
        )
    }
    print(
        f"Applying Team Changes to {len(football_channel_ids)} team channel(s)..."
    )
    for programme in root.iter("programme"):
        if programme.get("channel") not in football_channel_ids:
            continue
        for tag in ("title", "desc"):
            element = programme.find(tag)
            if element is not None and element.text:
                element.text = _rewrite_football_text(element.text)
                if tag == "title":
                    element.text = _replace_game_with_match(element.text)
        desc_el = programme.find("desc")
        description_key = football_programme_description_keys.get(id(programme))
        generated_description = event_descriptions.get(description_key or "")
        if desc_el is not None and generated_description:
            existing = (desc_el.text or "").strip()
            if generated_description not in existing:
                desc_el.text = f"{existing}\n\n{generated_description}" if existing else generated_description

    tree = ET.ElementTree(root)
    with gzip.open(
        OUTPUT_GZ_FILE, "wb", compresslevel=GZIP_COMPRESSLEVEL
    ) as destination:
        tree.write(destination, encoding="utf-8", xml_declaration=True)
    _save_ai_cache(ai_cache)
    print(f"Generated {OUTPUT_GZ_FILE.resolve()}")


if __name__ == "__main__":
    process_epg()
