"""
Collects event/banner statistics from the API, then feeds them
to GPT for next-event prediction.

Data collection ported from:
  web/backend/src/beta/lib/statistics_banner.php
  web/frontend/javascript/components/statisticsBanner.js
"""

import os
import sys
from io import BytesIO
from collections import Counter

import redis
import orjson
import requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.environ.get("API_BASE", "http://api.internal:5000")

MODEL = "gpt-5.5"
BANNER_UNITS = ["light_sound", "idol", "street", "theme_park", "school_refusal"]

# redis
r = redis.Redis(
    host="valkey.internal",
    port=6379,
    db=3,
    username="default",
    password="fixme",
    decode_responses=True
)
try:
    r.ping()
except redis.AuthenticationError:
    print("Redis authentication failed. Check your password.")
    sys.exit(1)
except redis.ConnectionError:
    print("Could not connect to Redis.")
    sys.exit(1)

# collect data

def get_unit_name(character_id: int) -> str:
    if 1 <= character_id <= 4:
        return "light_sound"
    if 5 <= character_id <= 8:
        return "idol"
    if 9 <= character_id <= 12:
        return "street"
    if 13 <= character_id <= 16:
        return "theme_park"
    if 17 <= character_id <= 20:
        return "school_refusal"
    return "piapro"


def most_common_first(values: list) -> list:
    counts = Counter(values)
    return [k for k, _ in counts.most_common()]


def fetch_json(path: str):
    url = f"{API_BASE}/json/{path}"
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.json()


def compute_statistics() -> dict:
    events = fetch_json("events.json")
    event_cards_list = fetch_json("eventCards.json")
    event_bonuses = fetch_json("eventDeckBonuses.json")

    cards = {}
    for card in fetch_json("cards.json"):
        cards[card["id"]] = {
            "attr": card["attr"],
            "characterId": card["characterId"],
        }

    event_cards_unit: dict[int, list[str]] = {}
    event_cards_character: dict[int, int] = {}
    for ec in event_cards_list:
        eid = ec["eventId"]
        if eid not in event_cards_unit:
            event_cards_unit[eid] = []
        if ec.get("isDisplayCardStory"):
            card_info = cards.get(ec["cardId"])
            if not card_info:
                continue
            char_id = card_info["characterId"]
            card_unit = get_unit_name(char_id)
            if card_unit == "piapro":
                continue
            if eid not in event_cards_character:
                event_cards_character[eid] = char_id
            event_cards_unit[eid].append(card_unit)

    event_bonus_list: dict[int, list[str]] = {}
    for eb in event_bonuses:
        eid = eb["eventId"]
        if eid not in event_bonus_list:
            event_bonus_list[eid] = []
        if eb.get("cardAttr"):
            event_bonus_list[eid].append(eb["cardAttr"])

    event_units: dict[int, str | None] = {}
    event_attrs: dict[int, str | None] = {}
    for eid, units in event_cards_unit.items():
        unique_units = list(set(units))
        if len(unique_units) < 1:
            continue
        event_units[eid] = None if len(unique_units) > 1 else unique_units[0]
        bonuses = event_bonus_list.get(eid, [])
        event_attrs[eid] = most_common_first(bonuses)[0] if bonuses else None

    event_final = {}
    chara_count: dict[int, int] = {}
    for event in events:
        eid = event["id"]
        if eid not in event_units:
            continue
        unit = event_units[eid]
        attr = event_attrs.get(eid)
        chara = event_cards_character.get(eid)
        event_type = event["eventType"]

        if chara not in chara_count:
            chara_count[chara] = 0
        if unit and attr:
            chara_count[chara] += 1

        event_final[eid] = {
            "unit": unit,
            "type": event_type,
            "attr": attr,
            "chara": chara,
            "chara_count": chara_count[chara],
        }

    return event_final


def extract_banner_data(event_final: dict):
    all_sorted = sorted(event_final.items(), key=lambda x: int(x[0]))

    unit_events = [
        (eid, ev) for eid, ev in all_sorted
        if ev["type"] != "world_bloom" and ev["unit"] and ev["chara"]
    ]

    chara_order = [ev["chara"] for _, ev in unit_events]

    by_unit: dict[str, list] = {}
    for _, ev in unit_events:
        by_unit.setdefault(ev["unit"], []).append(ev)

    tmp_result = {}
    for unit in BANNER_UNITS:
        tmp_result[unit] = [ev["chara"] for ev in by_unit.get(unit, [])]

    return chara_order, tmp_result


# ── prediction ───────────────────────────────────────────────────


input_text = """
ROLE:
You are an expert data analyst and event planning specialist working for the Japanese rhythm game "Project Sekai: Colorful Stage!".
Considering how large and how crazy their fandom is, very high chance that fans will come and destroy you if you hand out the wrong prediction data.

CONTEXT:
Two JSON files are provided detailing the chronological history of past game events based on units and characters.
The last entry in each list is the most recent event.

Character-to-unit mapping:
- light_sound: 1, 2, 3, 4
- idol: 5, 6, 7, 8
- street: 9, 10, 11, 12
- theme_park: 13, 14, 15, 16
- school_refusal: 17, 18, 19, 20

1. The first JSON shows the sequential order of which specific characters were the focus for events (separated unit-wise).
2. The second JSON shows the sequential order of which specific characters were the focus for all events that occurred previously in the game.

Mixed and other non-standard event types have been removed from the data to reduce noise.

PREDICTION RULES:
* Unit-wise rules (First JSON)
    * Rotation Cycles: Character focus rotations on each unit generally occur every 4 to 8 events.
    * Even Distribution: Within these time frames, each character within a unit is expected to have a relatively even appearance rate.
* All event rules (Second JSON)
    * All units occur at least once in every 5 event rotations.
      For example, from 115th event to 120th event, each unit has appeared once within the 5 event rotation.
      ```
      >>> print(all_events[115:120]) # idol, school_refusal, street, light_sound, theme_park
      [6, 17, 12, 4, 14]
      ```

* At most, you may pick maximum of two candidates from each possible unit for each upcoming next events.
* Priority (Overdue): Characters or units that have gone the longest without an event focus have the highest probability of appearing next.

TASK:
Analyze the provided JSON data and predict the next five upcoming events in chronological order (any unit).
For each event, provide the four most likely candidate characters.

OUTPUT FORMAT:
Output ONLY a valid JSON object.
The JSON must follow this exact structure
```
{
  "next_five_events": [
    {
      "candidates": [
        {"character_id": 1, "likelihood": 0.40},
        {"character_id": 2, "likelihood": 0.30},
        {"character_id": 3, "likelihood": 0.20},
        {"character_id": 4, "likelihood": 0.10}
      ]
    },
    {
      "candidates": [
        {"character_id": 5, "likelihood": 0.35},
        {"character_id": 6, "likelihood": 0.25},
        {"character_id": 7, "likelihood": 0.20},
        {"character_id": 8, "likelihood": 0.20}
      ]
    }
  ]
}
```
"""

output_schema = {
    "type": "object",
    "properties": {
        "next_five_events": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "character_id": {"type": "integer"},
                                "likelihood": {"type": "number"}
                            },
                            "required": ["character_id", "likelihood"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["candidates"],
                "additionalProperties": False
            }
        }
    },
    "required": ["next_five_events"],
    "additionalProperties": False
}


def make_upload(client: OpenAI, name: str, data: bytes):
    buf = BytesIO(data)
    buf.name = name
    return client.files.create(file=buf, purpose="user_data")


def main():
    # Collect data
    print("Fetching event data...")
    event_final = compute_statistics()
    print(f"Got {len(event_final)} events")

    chara_order, tmp_result = extract_banner_data(event_final)
    print(f"CHARA_ORDER: {len(chara_order)} entries")
    for unit, chars in tmp_result.items():
        print(f"  {unit}: {len(chars)} entries")

    # Upload to OpenAI via BytesIO
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    file1 = make_upload(client, "unit_events.json", orjson.dumps(tmp_result))
    file2 = make_upload(client, "all_events.json", orjson.dumps(chara_order))
    # Predict
    print("Requesting prediction...")
    response = client.responses.create(
        model=MODEL,
        instructions=(
            "Return only valid JSON matching the schema. "
            "For each upcoming event, provide exactly 4 unique candidate character_id values, "
            "ordered from highest to lowest likelihood. "
            "Likelihood values must be decimals between 0 and 1 and should sum to 1.0 for each event."
        ),
        reasoning={
            "effort": "medium",
            "summary": "auto"
        },
        max_output_tokens=300000,
        input=[
            {
                "role": "developer",
                "content": "Always follow the provided rules and instructions. Never make up missing facts."
            },
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": input_text},
                    {"type": "input_file", "file_id": file1.id},
                    {"type": "input_file", "file_id": file2.id},
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "event_prediction",
                "schema": output_schema,
                "strict": True,
            }
        },
    )

    script_dir = os.path.dirname(os.path.abspath(__file__))
    # with open(os.path.join(script_dir, "output.json"), "w", encoding="utf-8") as f:
    #     f.write(response.output_text)

    output_reasoning = []
    output_json = response.output_text

    for item in response.output:
        if getattr(item, "type", None) == "reasoning":
            for s in getattr(item, "summary", []):
                output_reasoning.append(getattr(s, "text", s))

    r.set("BANNER_PREDICTION_REASONING", orjson.dumps(output_reasoning))
    r.set("BANNER_PREDICTION_RESULT", orjson.dumps(output_json))

    print("\n".join(output_reasoning))

if __name__ == "__main__":
    main()
