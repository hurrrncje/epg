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
                event_title = next(
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
                }
                # Use the first matching marker alias; channel names are
                # normalized below regardless of their daily suffix.
                break

    # Some providers expose the event title only in the programme entries.
    for programme in root.findall("programme"):
        ch_id = programme.get("channel")
        if ch_id in targets and not targets[ch_id]["event_title"]:
            title = (programme.findtext("title") or "").strip()
            if title.casefold() != "no event":
                targets[ch_id]["event_title"] = title

    active_titles = sorted({
        item["event_title"] for item in targets.values()
        if item["event_title"] and item["start_times"]
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
        if not raw_title or not item["start_times"]:
            print(f"No scheduled listing for {item['core']} (no event or parseable start time).")
            continue
        result = cleaned_mapping.get(raw_title, {
            "clean_title": raw_title,
            "description": "Live event",
        })
        for event_time in item["start_times"]:
            start_eastern = datetime.combine(event_date, event_time, tzinfo=EASTERN)
            start_utc = start_eastern.astimezone(UTC)
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
