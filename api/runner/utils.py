"""Shared utilities for the event scoreboard runner scripts."""

import os
import time
import datetime
from typing import Optional

import requests
import mariadb


SQL_SERVER = "mariadb.internal"
API_SERVER = "http://api.internal:5000"

# Time buffers for event end detection.
# runner.py (5-min cron) allows a 16-minute window past aggregate time.
SCOREBOARD_END_BUFFER_5MIN = 960   # 16 minutes
# runner_current.py (1-min cron) allows a 12-minute window past aggregate time.
SCOREBOARD_END_BUFFER_1MIN = 720   # 12 minutes

# Bloom chapter active-window tolerance.
# runner.py uses a wide ±1 hour window to catch chapters that started/ended recently.
BLOOM_WINDOW_5MIN = 3600   # 60 minutes
# runner_current.py uses a tighter ±12 minute window.
BLOOM_WINDOW_1MIN = 720    # 12 minutes

# Sentinel rank value in "border rankings" API responses.
# The API includes rank 100 as a boundary marker; it is not a real ranker entry.
BORDER_RANK_SKIP = 100


def connect_db() -> mariadb.Connection:
    """Create and return a new MariaDB connection."""
    return mariadb.connect(
        host=SQL_SERVER,
        user="sekai",
        passwd="sekai",
        database="sekai",
    )


def fetch_card_asset(card_id: int, cache: dict, api_server: str = API_SERVER) -> Optional[str]:
    """Fetch the asset bundle name for a card from the API, with in-memory caching.

    Returns the asset name string if found, or None if the card is unknown
    or the request fails.
    """
    try:
        if cache.get(card_id):
            return cache[card_id]
        resp = requests.get(f"{api_server}/cards/{card_id}").json()
        if isinstance(resp, str) and resp.startswith("res"):
            cache[card_id] = resp
            return resp
        return None
    except Exception as e:
        print(f"[fetch_card_asset] card_id={card_id}: {e}")
        return None


def get_current_event(api_server: str = API_SERVER) -> dict:
    """Fetch current event metadata from the API.

    Raises on network failure or unexpected response — callers should let this
    propagate so the cron run exits visibly.
    """
    r = requests.get(f"{api_server}/current_event").json()
    event_start_int = int(r["startAt"] / 1000)
    event_end_int = int(r["aggregateAt"] / 1000)
    return {
        "event_id": r["id"],
        "event_name": r["name"],
        "event_start": event_start_int,
        "event_end": event_end_int,
        "event_start_str": str(datetime.datetime.fromtimestamp(event_start_int)),
        "event_end_str": str(datetime.datetime.fromtimestamp(event_end_int)),
    }


def get_current_bloom_characters(
    api_server: str = API_SERVER,
    window_seconds: int = BLOOM_WINDOW_1MIN,
) -> dict:
    """Fetch currently-active World Bloom chapter characters.

    Returns a dict mapping gameCharacterId -> eventId for chapters active within
    the given time window. Returns an empty dict on any failure.
    """
    try:
        r = requests.get(f"{api_server}/current_bloom").json()
        result = {}
        now = time.time()
        for chapter in r:
            chapter_start = chapter["chapterStartAt"] / 1000
            chapter_end = chapter["chapterEndAt"] / 1000
            chapter_character_id = chapter.get("gameCharacterId", 10000)
            if now >= chapter_start - window_seconds and now <= chapter_end + window_seconds:
                result[chapter_character_id] = chapter["eventId"]
        return result
    except Exception as e:
        print(f"[get_current_bloom_characters] {e}")
        return {}


def get_scoreboard(
    event_info: dict,
    api_server: str = API_SERVER,
    endpoint: str = "scoreboard/",
    end_buffer: int = SCOREBOARD_END_BUFFER_5MIN,
) -> Optional[dict]:
    """Fetch the live scoreboard from the public source.

    Returns None if the current time is outside the event window or if the
    scoreboard is already marked as aggregated. Raises on network failure.
    """
    now = time.time()
    if now < event_info["event_start"] or now > event_info["event_end"] + end_buffer:
        return None
    r = requests.get(f"{api_server}/{endpoint}").json()
    if r["top100"]["isEventAggregate"]:
        return None
    return r


def get_round(event_id: int, tmp_path: str) -> int:
    """Read the current round counter for an event from the tmp directory.

    Returns 0 if the file does not exist or cannot be parsed (e.g. first run).
    """
    try:
        with open(os.path.join(tmp_path, f"{int(event_id)}.txt"), "r") as f:
            return int(f.read())
    except (OSError, ValueError) as e:
        print(f"[get_round] event_id={event_id}: {e}")
        return 0


def set_round(event_id: int, value: int, tmp_path: str) -> None:
    """Write the current round counter for an event to the tmp directory."""
    try:
        with open(os.path.join(tmp_path, f"{int(event_id)}.txt"), "w") as f:
            f.write(str(value))
    except OSError as e:
        print(f"[set_round] event_id={event_id}: {e}")
        raise


def build_card_text(ranker: dict, cache: dict, api_server: str = API_SERVER) -> str:
    """Build the card info string from a ranker entry.

    Format: '{asset_name}/{level}/{master_rank}/{image_type}'
    """
    user_card = ranker.get("userCard", {})
    card_id = fetch_card_asset(user_card.get("cardId", 0), cache, api_server)
    level = user_card.get("level", "60")
    mr = user_card.get("masterRank", "0")
    img_type = user_card.get("defaultImage", "special_training")
    return f"{card_id}/{level}/{mr}/{img_type}"
