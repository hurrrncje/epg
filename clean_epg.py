"""Download an XMLTV EPG and clean titles on channels that mirror their name."""

from __future__ import annotations

import ast
import gzip
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

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

    print("Finding LIVE EVENT 01–36 channels and their current event titles...")
    channel_names: dict[str, str] = {}
    target_channel_ids: set[str] = set()
    target_labels: dict[str, str] = {}
    for channel in root.findall("channel"):
        ch_id = channel.get("id")
        if not ch_id:
            continue
        display_names = [(el.text or "").strip() for el in channel.findall("display-name")]
        if not display_names:
            continue
        channel_names[ch_id] = display_names[0]
        for display_name in display_names:
            marker = next(
                (prefix for prefix in TARGET_CHANNELS
                 if display_name == prefix or display_name.startswith(prefix + " ") or display_name.startswith(prefix + "-")),
                None,
            )
            if marker:
                target_channel_ids.add(ch_id)
                target_labels[ch_id] = display_name
                break

    # The current event title is the title in this channel's XMLTV programmes.
    titles_by_channel: dict[str, str] = {}
    for programme in root.findall("programme"):
        ch_id = programme.get("channel")
        title_el = programme.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if ch_id in target_channel_ids and title:
            titles_by_channel.setdefault(ch_id, title)

    titles = sorted(set(titles_by_channel.values()))
    print(f"Found {len(target_channel_ids)} LIVE EVENT 01–36 channel(s) and "
          f"{len(titles)} distinct current event title(s).")

    cleaned_mapping: dict[str, dict[str, str]] = {}
    for start in range(0, len(titles), BATCH_SIZE):
        batch = titles[start : start + BATCH_SIZE]
        print(f"Cleaning batch {start // BATCH_SIZE + 1} ({len(batch)} titles)...")
        cleaned_mapping.update(ai_clean_sport_titles(batch, client))

    print("Applying cleaned titles and descriptions...")
    # Update all schedule rows for selected channels, including repeated listings.
    for programme in root.findall("programme"):
        ch_id = programme.get("channel")
        raw_title = titles_by_channel.get(ch_id)
        result = cleaned_mapping.get(raw_title or "")
        if result is None:
            continue
        title_el = programme.find("title")
        if title_el is not None:
            title_el.text = result["clean_title"]
        desc_el = programme.find("desc")
        if desc_el is None:
            desc_el = ET.SubElement(programme, "desc")
        desc_el.text = result["description"]

    # Keep all other display-name aliases intact; update only the LIVE EVENT alias.
    for channel in root.findall("channel"):
        ch_id = channel.get("id")
        raw_title = titles_by_channel.get(ch_id)
        result = cleaned_mapping.get(raw_title or "")
        if result is None:
            continue
        for display_el in channel.findall("display-name"):
            if (display_el.text or "").strip() == target_labels.get(ch_id):
                display_el.text = result["clean_title"]

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(OUTPUT_XML_FILE, encoding="utf-8", xml_declaration=True)
    with OUTPUT_XML_FILE.open("rb") as source, gzip.open(OUTPUT_GZ_FILE, "wb", compresslevel=9) as destination:
        destination.write(source.read())
    print(f"Generated {OUTPUT_XML_FILE.resolve()} and {OUTPUT_GZ_FILE.resolve()}")


if __name__ == "__main__":
    process_epg()
