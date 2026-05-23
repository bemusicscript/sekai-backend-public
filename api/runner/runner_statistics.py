"""Statistics collector (30 minutes cron).

1. Fetch existing caches containing results
2. Fetch 100 users from latest scoreboard, minus the ones that are already fetched by jiiku runner
3. Use jiiku statistics and get results out
"""

import orjson
import time

import redis
import requests

from utils import (
    API_SERVER,
    BLOOM_WINDOW_5MIN,
    SCOREBOARD_END_BUFFER_1MIN,
    connect_db,
    get_current_event,
    get_current_bloom_characters,
)

JIIKU_SERVER = "http://jiiku-prod.internal:3000"
REDIS_HOST = "valkey.internal"
REDIS_PORT = 6379
REDIS_PASS = "fixme"

CACHE_TTL = 300       # 5 minutes
BATCH_SIZE = 10
PROFILE_TTL = 600     # 10 minutes, matches PHP load_profile

mysql = connect_db()
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASS,
    decode_responses=True,
    db=3
)


def type_to_str(scoreboard_type: int) -> str:
    return "all" if scoreboard_type == 0 else str(scoreboard_type)


def _build_graph_payloads(cur, profile_id, scoreboard_types, event_id, cards, team_power):
    """Build payload tuples for all scoreboard_types of a single player."""
    payloads = []
    for scoreboard_type in scoreboard_types:
        cur.execute(
            """
            SELECT scoreboard_updated, scoreboard_score
            FROM scoreboard_current
            WHERE scoreboard_profile_id = %s
            AND scoreboard_type = %s
            ORDER BY scoreboard_updated ASC
            """,
            (profile_id, scoreboard_type),
        )
        graph_rows = cur.fetchall()[::5]
        timestamps = [row[0] * 1000 for row in graph_rows]
        points = [row[1] for row in graph_rows]
        payloads.append((profile_id, scoreboard_type, {
            "team": {
                "cards": cards,
                "eventId": event_id,
                "teamPower": team_power,
            },
            "graph": {
                "timestamps": timestamps,
                "points": points,
            },
        }))
    return payloads


def _sync_profile_honors(cur, profile_id: int, profile_data: dict) -> None:
    """Port of PHP sync_profile_honors: INSERT IGNORE honor IDs for this profile."""
    honor_ids = list({
        h["honorId"]
        for src in ("userProfileHonors", "userHonors")
        for h in profile_data.get(src, [])
        if "honorId" in h
    })
    if not honor_ids:
        return
    cur.executemany(
        "INSERT IGNORE INTO profile_honors (scoreboard_profile_id, scoreboard_profile_honor_id) VALUES (%s, %s)",
        [(profile_id, hid) for hid in honor_ids],
    )


def _cards_from_api(profile_data: dict) -> list:
    """Build profile_decks list from response, matching PHP load_profile logic.

    Fetches card assets in one batch call and preserves member1..member5 deck order.
    """
    user_deck = profile_data.get("userDeck", {})
    cards_by_id = {c["cardId"]: c for c in profile_data.get("userCards", [])}

    # Collect member IDs in explicit deck order
    member_ids = [user_deck[f"member{i}"] for i in range(1, 6) if f"member{i}" in user_deck]

    # Fetch all card assets in one request
    card_assets = {}
    if member_ids:
        try:
            ids_str = ",".join(str(mid) for mid in member_ids)
            card_assets = requests.get(f"{API_SERVER}/cards/{ids_str}", timeout=5).json()
        except Exception as e:
            print(f"[runner_statistics] card asset fetch failed: {e}")

    deck_cards = []
    for mid in member_ids:
        card = cards_by_id.get(mid)
        if card:
            card = dict(card)
            card["cardAsset"] = card_assets.get(str(mid))
            deck_cards.append(card)
    return deck_cards


LAST_EVENT_KEY = "jiiku_statistics_last_event"


def collect_statistics() -> None:
    event_info = get_current_event()
    event_id = event_info["event_id"]
    # eid=0 matches the PHP convention for the current event in cache keys
    eid = 0
    cur = mysql.cursor()

    if int(time.time()) >= event_info["event_end"] + SCOREBOARD_END_BUFFER_1MIN:
        print("Event ended!")
        exit(0)

    # Step 0: Flush long cache from previous event if event changed
    last_event_id = r.get(LAST_EVENT_KEY)
    if last_event_id and int(last_event_id) != event_id:
        old_event_id = int(last_event_id)
        old_keys = list(r.scan_iter(f"jiiku_analysis_long:{old_event_id}:*", count=500))
        if old_keys:
            r.delete(*old_keys)
            print(f"[runner_statistics] flushed {len(old_keys)} long-cache keys for old event {old_event_id}")
    r.set(LAST_EVENT_KEY, event_id)

    # Step 1: Fetch active WL chapters and get overall (type=0) top-100 only.
    #         Use window_seconds=0 so only chapters literally running right now are included.
    #         BLOOM_WINDOW_5MIN is intentionally NOT used here — it would pull in recently
    #         finished chapters whose data is still in scoreboard_current.
    bloom_characters = get_current_bloom_characters(window_seconds=0)
    active_chapter_ids = set(bloom_characters.keys())
    is_world_link = bool(active_chapter_ids)

    latest_round_sql = "(SELECT scoreboard_round FROM scoreboard_current ORDER BY scoreboard_round DESC LIMIT 1)"

    cur.execute(
        f"""
        SELECT scoreboard_profile_id, scoreboard_type
        FROM scoreboard_current
        WHERE scoreboard_round={latest_round_sql}
        AND scoreboard_rank <= 100
        AND scoreboard_type = 0;
        """
    )
    players = cur.fetchall()
    print(f"[runner_statistics] {len(players)} overall (type=0) players" +
          (f", WL event with {len(active_chapter_ids)} active chapters" if is_world_link else ""))

    # For WL events, build chapter_type -> set(profile_id) mapping for snapshot step later
    chapter_players: dict = {}
    if is_world_link:
        fmt_ch = ", ".join(["%s"] * len(active_chapter_ids))
        cur.execute(
            f"""
            SELECT scoreboard_profile_id, scoreboard_type
            FROM scoreboard_current
            WHERE scoreboard_round={latest_round_sql}
            AND scoreboard_rank <= 100
            AND scoreboard_type IN ({fmt_ch});
            """,
            list(active_chapter_ids),
        )
        for profile_id, chapter_type in cur.fetchall():
            chapter_players.setdefault(chapter_type, set()).add(profile_id)
        print(f"[runner_statistics] chapter snapshot targets: { {t: len(p) for t, p in chapter_players.items()} }")

    # Step 2: Check Redis cache for each player, group uncached by profile_id
    cached_results: dict = {}
    uncached_results: dict = {}

    for profile_id, scoreboard_type in players:
        type_str = type_to_str(scoreboard_type)
        cache_key = f"jiiku_analysis:{eid}:{type_str}:{profile_id}"
        cached = r.get(cache_key)
        if cached is not None:
            cached_results[(profile_id, scoreboard_type)] = orjson.loads(cached)
        else:
            uncached_results.setdefault(profile_id, []).append(scoreboard_type)

    print(f"[runner_statistics] {len(cached_results)} cached, {len(uncached_results)} users uncached")

    # Step 3: Classify uncached profiles as fresh (use DB) / stale or missing (fetch API)
    player_payloads = []

    if uncached_results:
        fmt = ", ".join(["%s"] * len(uncached_results))
        cur.execute(
            f"""
            SELECT profile_id, profile_decks, profile_total_score, profile_updated
            FROM profile
            WHERE profile_id IN ({fmt}) AND profile_event_id={event_id}
            """,
            list(uncached_results.keys()),
        )
        db_profiles = {row[0]: row for row in cur.fetchall()}
        needs_api: set = set()
        now = int(time.time())
        fresh_count = 0

        for profile_id, scoreboard_types in uncached_results.items():
            row = db_profiles.get(profile_id)
            if row is None or now > int(row[3]) + PROFILE_TTL:
                needs_api.add(profile_id)
                continue

            # Fresh cached profile — use directly
            _, profile_decks, profile_total_score, _ = row
            cards = [
                {"cardId": int(card.get("cardId", 0)), "masterRank": int(card.get("masterRank", 0))}
                for card in (orjson.loads(profile_decks) or [])
            ]
            team_power = (orjson.loads(profile_total_score) or {}).get("totalPower", None)
            player_payloads.extend(
                _build_graph_payloads(cur, profile_id, scoreboard_types, event_id, cards, team_power)
            )
            fresh_count += 1

        print(f"[runner_statistics] {fresh_count} from DB cache, {len(needs_api)} need API fetch")

        # Step 4: Fetch from api.internal for stale/missing profiles, upsert into DB
        for profile_id in needs_api:
            print(f"[runner_statistics] fetching profile {profile_id} ({'new' if profile_id not in db_profiles else 'stale'})")
            try:
                profile_data = requests.get(f"{API_SERVER}/profile/{profile_id}", timeout=5).json()
            except Exception as e:
                print(f"[runner_statistics] profile fetch failed for {profile_id}: {e}")
                continue

            # Guard against error responses (e.g. maintenance / invalid user)
            if not profile_data.get("userProfile"):
                print(f"[runner_statistics] skipping {profile_id}: invalid profile response")
                continue

            _sync_profile_honors(cur, profile_id, profile_data)

            profile_decks = _cards_from_api(profile_data)
            total_power = profile_data.get("totalPower")
            nickname = profile_data.get("user", {}).get("name", "")
            updated_at = int(time.time())
            decks_json = orjson.dumps(profile_decks).decode()
            power_json = orjson.dumps(total_power).decode()

            cur.execute(
                """
                INSERT INTO profile VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    profile_nickname = VALUES(profile_nickname),
                    profile_decks = VALUES(profile_decks),
                    profile_total_score = VALUES(profile_total_score),
                    profile_updated = VALUES(profile_updated),
                    profile_event_id = VALUES(profile_event_id)
                """,
                (profile_id, nickname, decks_json, power_json, updated_at, event_id),
            )
            mysql.commit()

            cards = [
                {"cardId": int(c.get("cardId", 0)), "masterRank": int(c.get("masterRank", 0))}
                for c in profile_decks
            ]
            team_power_val = total_power.get("totalPower") if isinstance(total_power, dict) else total_power
            player_payloads.extend(
                _build_graph_payloads(cur, profile_id, uncached_results[profile_id], event_id, cards, team_power_val)
            )

        print(f"[runner_statistics] {len(player_payloads)} payloads queued for jiiku")

    # Step 5: Send in batches of BATCH_SIZE to jiiku-dev.internal
    new_results: dict = {}
    total_batches = (len(player_payloads) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(player_payloads), BATCH_SIZE):
        batch = player_payloads[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        try:
            resp = requests.post(
                f"{JIIKU_SERVER}/api/analyze_player",
                json={"players": [p[2] for p in batch], "internal": True},
                timeout=10,
            )
            data = resp.json()
        except Exception as e:
            print(f"[runner_statistics] jiiku batch {batch_num}/{total_batches} failed: {e}")
            continue

        result_players = data.get("players", [])
        for j, (profile_id, scoreboard_type, _) in enumerate(batch):
            if j >= len(result_players) or result_players[j] is None:
                continue
            new_results[(profile_id, scoreboard_type)] = result_players[j]

        print(f"[runner_statistics] jiiku batch {batch_num}/{total_batches} done ({len(result_players)} results)")

    # Step 6: Store new results — short TTL for live lookups, persistent long cache
    for (profile_id, scoreboard_type), result in new_results.items():
        type_str = type_to_str(scoreboard_type)
        encoded = orjson.dumps(result)
        r.setex(f"jiiku_analysis:{eid}:{type_str}:{profile_id}", CACHE_TTL, encoded)
        r.set(f"jiiku_analysis_long:{event_id}:{type_str}:{profile_id}", encoded)

    # Step 7: Refresh long cache for players that were already in short-TTL cache
    for (profile_id, scoreboard_type), result in cached_results.items():
        type_str = type_to_str(scoreboard_type)
        r.set(f"jiiku_analysis_long:{event_id}:{type_str}:{profile_id}", orjson.dumps(result))

    # Step 8: WL snapshot — flush active chapter long-cache keys, then repopulate from type=0 results.
    #         Finished chapters are untouched. Active chapters are fully refreshed each run.
    #         Chapter-typed entries are also added to new_results so step 9 feeds them to compute_stats.
    if chapter_players:
        type0_results: dict = {}
        for (profile_id, scoreboard_type), result in {**cached_results, **new_results}.items():
            if scoreboard_type == 0:
                type0_results[profile_id] = result

        snapshot_count = 0
        for chapter_type, profile_ids in chapter_players.items():
            type_str = type_to_str(chapter_type)

            # Flush this chapter's stale long-cache entries before rewriting
            old_keys = list(r.scan_iter(f"jiiku_analysis_long:{event_id}:{type_str}:*", count=200))
            if old_keys:
                r.delete(*old_keys)

            for profile_id in profile_ids:
                result = type0_results.get(profile_id)
                if result is None:
                    continue
                encoded = orjson.dumps(result)
                r.setex(f"jiiku_analysis:{eid}:{type_str}:{profile_id}", CACHE_TTL, encoded)
                r.set(f"jiiku_analysis_long:{event_id}:{type_str}:{profile_id}", encoded)
                new_results[(profile_id, chapter_type)] = result
                snapshot_count += 1
        print(f"[runner_statistics] WL snapshot: {snapshot_count} chapter keys written")

    print(f"[runner_statistics] done — {len(new_results)} new + {len(cached_results)} restored to long cache")

    # Step 9. compute all stats to Jiiku's compute_stats, grouped by type
    merged = {**cached_results, **new_results}
    by_type: dict = {}
    for (profile_id, scoreboard_type), result in merged.items():
        by_type.setdefault(scoreboard_type, []).append(result)

    for scoreboard_type, results in by_type.items():
        type_str = type_to_str(scoreboard_type)
        print(f"[runner_statistics] sending {len(results)} players to compute_stats (type={type_str})")
        payload = {
            "analyze_player_response": {
                "status": {"code": 0, "msg": "Success"},
                "players": results
            },
            "internal": True
        }
        try:
            resp = requests.post(
                f"{JIIKU_SERVER}/api/compute_stats",
                json=payload,
                timeout=10,
            )
            stats_data = resp.json()
            r.set(f"jiiku_analysis_stats:{event_id}:{type_str}", orjson.dumps(stats_data))
            print(f"[runner_statistics] compute_stats type={type_str} stored")
        except Exception as e:
            print(f"[runner_statistics] compute_stats type={type_str} failed: {e}")

    cur.close()


if __name__ == "__main__":
    collect_statistics()
    print("Done")
    mysql.close()
