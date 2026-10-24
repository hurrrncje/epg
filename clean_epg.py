"""Generate daily event listings for XMLTV channels LIVE EVENT 01–36."""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types


EPG_URL = "https://opop.pro/JKmjctL4MbfezAX"
OUTPUT_XML_FILE = Path("cleaned_epg.xml")
OUTPUT_GZ_FILE = Path("cleaned_epg.xml.gz")
GEMINI_MODEL = "gemini-3.8-flash"
BATCH_SIZE = 50
TARGET_CHANNELS = {f"LIVE EVENT {number:02d}" for number in range(1, 37)}
MAX_RETRIES = 3
REQUEST_TIMEOUT = (10, 120)
EASTERN = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
GMT = UTC
TIME_PATTERN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
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
EPL_CHANNEL_PATTERN = re.compile(r"^EPL(?P<number>0[1-9]|10):\s*(?P<details>.*)$", re.IGNORECASE)
EPL_FIXTURE_PATTERN = re.compile(
    r"^(?P<home>.+?)\s+(?P<clock>(?:[01]?\d|2[0-3]):[0-5]\d)\s+"
    r"(?P<away>.+?)\s+(?P<day>\d{1,2})/(?P<month>\d{1,2})"
    r"(?:/(?P<year>\d{4}))?$"
)


def _event_number_and_suffix(display_name: str) -> tuple[str, str] | None:
    """Return the stable LIVE EVENT NN name and its changing suffix."""
    match = re.match(r"^(LIVE EVENT \d{2})(?=$|[\s-])(.*)$", display_name.strip(), re.IGNORECASE)
    if not match:
        return None
    core = match.group(1).upper()
    if core not in TARGET_CHANNELS:
        return None
    return core, match.group(2).strip().lstrip("- ").strip()


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


def _parse_epl_dynamic_label(label: str, today: Any) -> dict[str, Any] | None:
    """Parse labels such as `EPL01: Aston Villa 12:30 Man City 24/10`."""
    channel_match = EPL_CHANNEL_PATTERN.match(label.strip())
    if not channel_match:
        return None
    number = int(channel_match.group("number"))
    details = channel_match.group("details").strip()
    result: dict[str, Any] = {
        "channel_name": f"EPL{number:02d}",
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


def _parse_model_response(response: Any) -> list[dict[str, str]]:
    """Parse the model's requested Python-list format, tolerating code fences."""
    text = getattr(response, "text", None) or ""
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
        raise ValueError("Gemini response was not a list")
    for item in result:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str)
            for key in ("raw", "clean_title", "description")
        ):
            raise ValueError("Gemini returned an item with missing or invalid fields")
    return result


def ai_clean_sport_titles(channel_names: list[str], client: genai.Client) -> dict[str, dict[str, str]]:
    """Ask Gemini to clean a batch of mirrored event titles."""
    if not channel_names:
        return {}

    prompt = """You organize sports event titles for an IPTV electronic program guide.
For every input string, return its exact original value in `raw`, a concise
properly formatted `clean_title`, and a brief factual `description`. Do not
invent teams, competitions, or event details that cannot be inferred. Return
only a JSON array of objects with exactly these string fields:
raw, clean_title, description.

Input titles (JSON):
""" + json.dumps(channel_names, ensure_ascii=False)

    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
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
            if attempt + 1 < MAX_RETRIES:
                time.sleep(min(2 ** attempt, 8))
    print("Skipping this batch after repeated failures.")
    return {}


def process_epg() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is required.")
    client = genai.Client(api_key=api_key)

    print("Downloading EPG XML...")
    response = requests.get(EPG_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    # XMLTV documents may declare a non-UTF-8 encoding; parse the response bytes.
    root = ET.fromstring(response.content)

    print("Finding LIVE EVENT 01–36 channels, current events, and start times...")
    targets: dict[str, dict[str, Any]] = {}
    for channel in root.findall("channel"):
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
                event_title = permanent_title or next(
                    (name for name in display_names
                     if not _event_number_and_suffix(name)
                     and name.casefold() != "no event"),
                    "",
                )
                times = _parse_eastern_starts(suffix)
                targets[ch_id] = {
                    "core": core,
                    "event_title": event_title,
                    "start_times": times,
                    "permanent": bool(permanent_title),
                }
                # Use the first matching marker alias; channel names are
                # normalized below regardless of their daily suffix.
                break

    epl_today = datetime.now(LONDON).date()
    epl_targets: dict[str, dict[str, Any]] = {}
    for channel in root.findall("channel"):
        ch_id = channel.get("id")
        if not ch_id:
            continue
        for display_el in channel.findall("display-name"):
            parsed = _parse_epl_dynamic_label((display_el.text or "").strip(), epl_today)
            if parsed:
                epl_targets[ch_id] = parsed
                break

    # Some providers expose the event title only in the programme entries.
    for programme in root.findall("programme"):
        ch_id = programme.get("channel")
        if ch_id in targets and not targets[ch_id]["event_title"]:
            title = (programme.findtext("title") or "").strip()
            if title.casefold() != "no event":
                targets[ch_id]["event_title"] = title
        if ch_id and ch_id not in epl_targets:
            parsed = _parse_epl_dynamic_label(
                (programme.findtext("title") or "").strip(), epl_today
            )
            if parsed:
                epl_targets[ch_id] = parsed

    active_titles = sorted({
        item["event_title"] for item in targets.values()
        if item["event_title"] and item["start_times"] and not item["permanent"]
    })
    print(f"Found {len(targets)} target channel(s), "
          f"{len(active_titles)} active event title(s) with parseable start times.")

    cleaned_mapping: dict[str, dict[str, str]] = {}
    for start in range(0, len(active_titles), BATCH_SIZE):
        batch = active_titles[start : start + BATCH_SIZE]
        print(f"Cleaning batch {start // BATCH_SIZE + 1} ({len(batch)} titles)...")
        cleaned_mapping.update(ai_clean_sport_titles(batch, client))

    # Strip all changing event details from display names. Repeated aliases are
    # retained to preserve the XML structure, but contain only the stable label.
    for channel in root.findall("channel"):
        item = targets.get(channel.get("id"))
        if item:
            for display_el in channel.findall("display-name"):
                display_el.text = item["core"]

    # Replace provider placeholder rows on these channels with generated listings.
    target_ids = set(targets)
    for programme in root.findall("programme"):
        if programme.get("channel") in target_ids:
            root.remove(programme)

    event_date = datetime.now(EASTERN).date()
    generated: list[ET.Element] = []
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
        if not raw_title or not item["start_times"]:
            print(f"No scheduled listing for {item['core']} (no event or parseable start time).")
            continue
        result = cleaned_mapping.get(raw_title, {
            "clean_title": raw_title,
            "description": "Live event",
        })
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
            f"Upcoming Event: {result['clean_title']} "
            f"{first_start.strftime('%H:%M')} GMT"
        )
        if upcoming_start < first_start and datetime.now(UTC) < first_start:
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": first_start.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = upcoming_title
            generated.insert(channel_generated_start, upcoming)

    # Replace repeating source placeholders on the dynamic EPL01–EPL10 channels.
    for programme in root.findall("programme"):
        if programme.get("channel") in epl_targets:
            root.remove(programme)

    now_utc = datetime.now(UTC)
    for ch_id, item in epl_targets.items():
        title = item["title"]
        start_utc = item["start_utc"]
        if not title or start_utc is None:
            print(f"No fixture listing for {item['channel_name']} (empty or unparsed label).")
            continue
        end_utc = start_utc + timedelta(minutes=150)
        upcoming_start = datetime.combine(start_utc.date(), datetime_time.min, tzinfo=UTC)
        upcoming_title = f"Upcoming Event: {title} {start_utc.strftime('%H:%M')} GMT"
        if upcoming_start < start_utc and now_utc < start_utc:
            upcoming = ET.Element("programme", {
                "start": upcoming_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": start_utc.strftime("%Y%m%d%H%M%S %z"),
                "channel": ch_id,
            })
            ET.SubElement(upcoming, "title").text = upcoming_title
            ET.SubElement(upcoming, "desc").text = upcoming_title
            generated.append(upcoming)
        fixture_programme = ET.Element("programme", {
            "start": start_utc.strftime("%Y%m%d%H%M%S %z"),
            "stop": end_utc.strftime("%Y%m%d%H%M%S %z"),
            "channel": ch_id,
        })
        ET.SubElement(fixture_programme, "title").text = title
        ET.SubElement(fixture_programme, "desc").text = title
        generated.append(fixture_programme)

    first_programme_index = next(
        (index for index, element in enumerate(root) if element.tag == "programme"),
        len(root),
    )
    for offset, programme in enumerate(generated):
        root.insert(first_programme_index + offset, programme)

    football_channel_ids = {
        channel.get("id")
        for channel in root.findall("channel")
        if (channel.get("id") or "").upper().startswith(("EPL", "EFL"))
    }
    print(f"Rewriting fixtures for {len(football_channel_ids)} EPL/EFL channel(s)...")
    for programme in root.findall("programme"):
        if programme.get("channel") not in football_channel_ids:
            continue
        for tag in ("title", "desc"):
            element = programme.find(tag)
            if element is not None and element.text:
                element.text = _rewrite_football_text(element.text)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(OUTPUT_XML_FILE, encoding="utf-8", xml_declaration=True)
    with OUTPUT_XML_FILE.open("rb") as source, gzip.open(OUTPUT_GZ_FILE, "wb", compresslevel=9) as destination:
        destination.write(source.read())
    print(f"Generated {OUTPUT_XML_FILE.resolve()} and {OUTPUT_GZ_FILE.resolve()}")


if __name__ == "__main__":
    process_epg()
