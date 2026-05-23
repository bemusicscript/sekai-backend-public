#!/usr/bin/python -u
# pylint: disable=no-member, missing-function-docstring, consider-using-with, bare-except

"""
app.py

Main backend for Sekai
"""

from contextlib import asynccontextmanager
from functools import wraps
import os
import pathlib
import time
import logging
import asyncio
import orjson
import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse
from aiocache import Cache
from aiocache.serializers import PickleSerializer
from api.crawler import (
    DiscordUser,
    DiscordSekaiBridge,
    close_client,
)

# Logging #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [worker:%(process)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

# Configurations #

APP_VERSION = "v5.0-20260302"
CURRENT_PATH = os.path.dirname(os.path.realpath(__file__))
DATABASE_DIR = os.path.join(CURRENT_PATH, "database", "json")
PREDICT_DIR  = os.path.join(CURRENT_PATH, "..", "prediction")

# Custom JSON response using orjson (replaces deprecated ORJSONResponse) #
class ORJSONResponse(Response):
    """
    Custom JSON response using orjson
    -> This effectively replaces deprecated ORJSONResponse
    """
    media_type = "application/json"

    def render(self, content) -> bytes:
        return orjson.dumps(
            content, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY
        )

# FastAPI Init #
async def init_cache(app):
    app.async_cache = aioredis.Redis(
        host="valkey.internal",
        port=6379,
        password="fixme",
        decode_responses=True
    )
    app.cache = Cache(
        Cache.REDIS,
        endpoint="valkey.internal",
        port=6379,
        password="fixme",
        namespace="main",
        serializer=PickleSerializer(protocol=5),
    )

@asynccontextmanager
async def lifespan(app):
    """
    Close all asyncio http clients upon exit, to prevent memory leaks
    """
    await init_cache(app)
    yield
    await app.async_cache.close()
    await close_client()

app = FastAPI(
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Multiprocessing Cache #

"""
aiocache (PickleSerializer) is used for simple shared data: scoreboard, cards,
event info, predict, bloom. Auth state (session token + headers) is stored
separately in a plain Redis hash via app.async_cache to avoid pickling
DiscordSekaiBridge objects across workers.
"""

app.timeout = {
    "session": 60 * 10,
    "force_reset_cooldown": 10,
    "scoreboard": 60,
    "predict": 60 * 10,
    "cards": 60 * 60,
    "current_event": 60 * 60,
    "current_bloom": 60 * 60,
    "current_cheerful": 60 * 30,
}

class RedisCache:
    """
    Redis aiocache
    """

    def __getattr__(self, name):
        """
        gets data from cache, otherwise return none values.
        """
        none_value = {}

        if name.endswith("_updated") or name.endswith("_eid"):
            none_value = -1
        if name.endswith("cards"):
            none_value = []

        return app.cache.get(name, none_value)

    async def set(self, name, value):
        """ set redis cache """
        return await app.cache.set(name, value)

app.redis_cache = RedisCache()


# Helper functions #


def _sync_read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

async def _read_file(path: str) -> bytes:
    """ Non-blocking file read via thread pool """
    return await asyncio.to_thread(_sync_read_file, path)


async def _build_session() -> DiscordSekaiBridge:
    """
    Reconstruct a DiscordSekaiBridge session from the auth state stored in Redis.
    No pickle — only plain strings are stored and restored.
    """
    r = app.async_cache
    user = DiscordUser(
        await r.get("DISCORD_UID"),
        await r.get("DISCORD_TOKEN"),
        await r.get("DISCORD_CHANNEL_ID"),
    )
    session = DiscordSekaiBridge(user)
    auth = await r.hgetall("auth_state")
    if auth:
        token = auth.get("session_token", "")
        session.token = token
    return session


async def _save_auth_state(session: DiscordSekaiBridge):
    """
    After a successful game API call, push the new rolling token into the token
    queue and update version headers in auth_state.

    The token queue (a Redis list) holds exactly one token at a time.  Workers
    BLPOP to claim it exclusively before making a game API call, then RPUSH the
    new token returned in the response.  This serializes token usage across all
    workers and eliminates rolling-token race conditions.
    """
    token = session.get("token", session.token)
    if not token:
        return
    await app.async_cache.rpush("rolling_token_queue", token)
    await app.async_cache.hset("auth_state", mapping={
        "session_token": token,
    })


# Lua script: push auth_state.session_token into rolling_token_queue only when
# the queue is empty (atomic check-and-push to avoid duplicates on startup).
_POPULATE_QUEUE_LUA = """
if redis.call('LLEN', KEYS[1]) == 0 then
    local token = redis.call('HGET', KEYS[2], 'session_token')
    if token and token ~= '' then
        redis.call('RPUSH', KEYS[1], token)
        return 1
    end
end
return 0
"""


async def _api_call_with_retry(call_fn):
    """
    Claims the rolling token from a Redis list via BLMOVE (reliable queue),
    injects it into the session, calls call_fn(session), and on success pushes
    the new rolling token back so the next worker can claim it.

    BLMOVE atomically moves the token from rolling_token_queue →
    rolling_token_processing.  If the worker crashes mid-call the token stays
    in rolling_token_processing; the next caller that times out on BLMOVE will
    detect it, discard it, and trigger a reauth — bounding the outage to the
    BLMOVE timeout (3 s) rather than waiting 15 s or indefinitely.

    Because only one token exists in the queue at a time, only one worker can
    hold a given token — rolling-token races are impossible.
    """
    session = await _build_session()
    result = {"httpStatus": 403}   # sentinel to enter the loop
    retry_count = 0

    while result.get("httpStatus") in (426, 403) and retry_count < 5:
        # Linear backoff between retries: gives the reauth/queue time to recover.
        if retry_count > 0:
            await asyncio.sleep(0.1 * retry_count)

        # Atomically move token to processing list (reliable queue pattern).
        # timeout=3 keeps us safely within the PHP 5 s upstream timeout.
        token = await app.async_cache.blmove(
            "rolling_token_queue", "rolling_token_processing",
            timeout=3, src="LEFT", dest="RIGHT",
        )
        if token is None:
            # Nothing arrived in 3 s.  Check whether a previous worker crashed
            # and left a stale token in the processing list.
            stuck = await app.async_cache.lrange("rolling_token_processing", 0, -1)
            if stuck:
                logging.warning("_api_call_with_retry: stale token in processing list, clearing and reauthenticating")
                await app.async_cache.delete("rolling_token_processing")
            # Reauth pushes a fresh token into rolling_token_queue.
            try:
                await update_authentication(True)
            except Exception:
                logging.exception("_api_call_with_retry: reauth failed, will retry")
            # Bootstrap fallback for restart with valid-but-queue-empty auth.
            await app.async_cache.eval(
                _POPULATE_QUEUE_LUA, 2, "rolling_token_queue", "auth_state"
            )
            retry_count += 1
            continue

        session.token = token

        try:
            result = await call_fn(session)
        except Exception:
            result = {"httpStatus": 426}

        # Remove from processing list regardless of outcome.
        await app.async_cache.lrem("rolling_token_processing", 1, token)

        if result.get("httpStatus") not in (403, 426):
            await _save_auth_state(session)   # pushes new token to queue
        else:
            # Token consumed by server with no usable response; reauth to get
            # a fresh token pushed into the queue for the next attempt.
            try:
                await update_authentication(True)
            except Exception:
                logging.exception("_api_call_with_retry: reauth failed, will retry")

        retry_count += 1

    if result.get("httpStatus") in (403, 426):
        return {}
    return result


async def _get_cheerful_info(event_id: int) -> dict:
    """
    Fetch and parse cheerful carnival information for the given event ID.
    """
    timeout = (await app.redis_cache.current_cheerful_updated) + app.timeout["current_cheerful"]
    if time.time() >= timeout:
        cheerful_summary_data = await _read_file(os.path.join(DATABASE_DIR, "cheerfulCarnivalSummaries.json"))
        cheerful_teams_data = await _read_file(os.path.join(DATABASE_DIR, "cheerfulCarnivalTeams.json"))
        await app.redis_cache.set("current_cheerful_summary", cheerful_summary_data)
        await app.redis_cache.set("current_cheerful_teams", cheerful_teams_data)
        await app.redis_cache.set("current_cheerful_updated", time.time())

    cheerful_summary_data = orjson.loads(await app.redis_cache.current_cheerful_summary)
    cheerful_teams_data = orjson.loads(await app.redis_cache.current_cheerful_teams)

    try:
        current_cheerful_summary = next(item for item in cheerful_summary_data if item["eventId"] == event_id)
        current_cheerful_summary = {
            'theme': current_cheerful_summary['theme'],
            'midtermAnnounce1At': current_cheerful_summary['midtermAnnounce1At'],
            'midtermAnnounce2At': current_cheerful_summary['midtermAnnounce2At']
        }
        current_cheerful_teams = list(filter(lambda item: item['eventId'] == event_id, cheerful_teams_data))
        return {'summary': current_cheerful_summary, 'teams': current_cheerful_teams}
    except Exception as e:
        logging.warning("_get_cheerful_info: error - %s", e)
        return {'summary': {}, 'teams': []}


async def _load_event_data() -> list:
    """
    Load event list from cache, refreshing from disk when expired.
    """
    timeout = (await app.redis_cache.current_event_updated) + app.timeout["current_event"]
    if time.time() >= timeout:
        event_data = await _read_file(os.path.join(DATABASE_DIR, "events.json"))
        await app.redis_cache.set("current_event", event_data)
        await app.redis_cache.set("current_event_updated", time.time())
    return orjson.loads(await app.redis_cache.current_event)


# Update Database #


async def update_database():
    """
    Update asset database and write to output
    """
    await update_authentication()
    result = await _api_call_with_retry(lambda s: s.check_database())
    print(result)

    for db_key, db_data in result.items():
        db_filename = os.path.join(DATABASE_DIR, db_key + ".json")
        with open(db_filename, "wb") as content:
            content.write(orjson.dumps(db_data))

    return True


async def update_authentication(force_reset=False):
    """
    Ensure a valid session exists in Redis for all workers.

    - Auth state is stored as a Redis hash (no pickle), so workers share only
      plain strings: session_token, version headers, and updated_at timestamp.
    - A distributed Redis lock prevents multiple workers from re-authenticating
      simultaneously (thundering herd). A double-check after lock acquisition
      avoids redundant logins when another worker already renewed the session.
    - Cards are refreshed independently of the session on their own TTL.
    """
    r = app.async_cache
    auth = await r.hgetall("auth_state")
    auth_age = time.time() - float(auth.get("updated_at", 0)) if auth else float("inf")
    session_expired = (
        not auth or
        auth_age >= app.timeout["session"] or
        (force_reset and auth_age >= app.timeout["force_reset_cooldown"])
    )

    if session_expired:
        logging.warning(
            "update_authentication: reauthenticating (%s)",
            "force reset" if force_reset else "session timeout",
        )
        async with r.lock("auth_lock", timeout=30, blocking_timeout=15):
            # Double-check: re-read auth state since another worker may have
            # already re-authed while we waited for the lock.
            auth = await r.hgetall("auth_state")
            auth_age = time.time() - float(auth.get("updated_at", 0)) if auth else float("inf")
            if not auth or auth_age >= app.timeout["session"] or (force_reset and auth_age >= app.timeout["force_reset_cooldown"]):
                user = DiscordUser(
                    await r.get("DISCORD_UID"),
                    await r.get("DISCORD_TOKEN"),
                    await r.get("DISCORD_CHANNEL_ID"),
                )
                session = DiscordSekaiBridge(user)
                await session.login()
                fresh_token = session.get("token", session.token)
                await r.hset("auth_state", mapping={
                    "session_token": fresh_token,
                    "updated_at": str(time.time()),
                })
                # Atomically clear any stale tokens and push the single fresh
                # token.  DEL+RPUSH as one Lua script prevents _save_auth_state
                # from inserting a second token between the two commands.
                await r.eval(
                    "redis.call('DEL', KEYS[1]); redis.call('RPUSH', KEYS[1], ARGV[1])",
                    1, "rolling_token_queue", fresh_token
                )

    # Renew cards independently (updated by update_database on its own schedule)
    if time.time() >= (await app.redis_cache.cards_updated) + app.timeout["cards"]:
        cards = orjson.loads(await _read_file(os.path.join(DATABASE_DIR, "cards.json")))
        await app.redis_cache.set("cards", {card["id"]: card["assetbundleName"] for card in cards})
        await app.redis_cache.set("cards_updated", time.time())


def update_auth_required(func):
    """
    Wrapper function that calls update_authentication()
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        await update_authentication()
        return await func(*args, **kwargs)

    return wrapper


# API Endpoints #


@app.get("/scoreboard/")
@update_auth_required
async def current_scoreboard():
    """
    Scoreboard endpoint
    Returns scoreboard information
    """
    event = await current_event()
    event_id = event['id']

    scoreboard = await app.redis_cache.scoreboard

    if not scoreboard:
        scoreboard = {
            "data": None,
            "last_updated": -1,
        }
        await app.redis_cache.set("scoreboard", scoreboard)

    expiry_date = scoreboard["last_updated"] + app.timeout["scoreboard"]

    result = {"top100": {}, "highlight": {}}
    if time.time() >= expiry_date:
        result["top100"] = await _api_call_with_retry(
            lambda s: s.get_top100(event_id)
        )
        result["highlight"] = await _api_call_with_retry(
            lambda s: s.get_highlight(event_id)
        )

        scoreboard["data"] = result
        scoreboard["last_updated"] = int(time.time())
        await app.redis_cache.set("scoreboard", scoreboard)

    else:
        result = scoreboard["data"]

    return result



@app.get("/profile/{friend_code}")
@update_auth_required
async def get_profile(friend_code) -> dict:
    """
    Check profile information
    """
    return await _api_call_with_retry(
        lambda s: s.get_user_profile(str(friend_code))
    )


@app.get("/cards/{card_id}")
@update_auth_required
async def card_data(card_id: str):
    """
    Retrieve assetbundleName from cards
    """
    cards = await app.redis_cache.cards

    # for a single card
    if card_id.isdigit():
        return cards.get(int(card_id), None)

    # for multiple cards
    target_cards = card_id.split(",")
    if not target_cards:
        return {}

    result = {}
    for target_card in target_cards:
        if not target_card.isdigit():
            continue
        result[int(target_card)] = cards.get(int(target_card), None)

    return result


@app.get("/current_event")
async def current_event() -> dict:
    """
    Fetch latest event information from asset data
    Using orjson for best performance
    """
    event_data = await _load_event_data()

    now = time.time()
    possible_events = [
        event for event in event_data
        if (now >= event['eventOnlyComponentDisplayStartAt'] / 1000 and
            now <= event['eventOnlyComponentDisplayEndAt'] / 1000)
    ]
    current = possible_events[-1] if possible_events else event_data[-1]

    current['cheerfulInfo'] = {}
    if current['eventType'] == "cheerful_carnival":
        current['cheerfulInfo'] = await _get_cheerful_info(current['id'])

    return current


@app.get("/current_event/{event_id}")
async def current_event_custom(event_id) -> dict:
    """
    Fetch latest event information from asset data
    """
    if not str(event_id).isdigit():
        return {}

    event_data = await _load_event_data()

    current = next((e for e in event_data if e['id'] == int(event_id)), None)
    if current is None:
        return {}

    current['cheerfulInfo'] = {}
    if current['eventType'] == "cheerful_carnival":
        current['cheerfulInfo'] = await _get_cheerful_info(current['id'])

    return current


@app.get("/json/{filename}")
async def get_asset_data(filename: str):
    """
    Fetch JSON data
    """
    fn = os.path.basename(filename)
    fn_abs = pathlib.Path(DATABASE_DIR, fn).resolve()

    if not fn_abs.is_relative_to(DATABASE_DIR):
        return Response(content="{}", media_type="application/json")

    if fn_abs.is_file():
        return Response(content=await _read_file(fn_abs), media_type="application/json")

    return Response(content="{}", media_type="application/json")


@app.get("/current_bloom", response_class=PlainTextResponse)
async def current_bloom():
    """
    Fetch bloom information
    Using plaintext for best performance
    """
    timeout = (await app.redis_cache.current_bloom_updated) + app.timeout["current_bloom"]
    if time.time() >= timeout:
        event_data = await _read_file(os.path.join(DATABASE_DIR, "worldBlooms.json"))
        await app.redis_cache.set("current_bloom", event_data)
        await app.redis_cache.set("current_bloom_updated", time.time())

    return await app.redis_cache.current_bloom


@app.get("/predict/", response_class=PlainTextResponse)
async def predict():
    """
    Prediction dataset
    Using plaintext for best performance
    """
    timeout = (await app.redis_cache.predict_updated) + app.timeout["predict"]
    if time.time() >= timeout:
        predict_data = await _read_file(os.path.join(PREDICT_DIR, "result.json"))
        await app.redis_cache.set("predict", predict_data)
        await app.redis_cache.set("predict_updated", time.time())

    return await app.redis_cache.predict


@app.get("/reset/", response_class=PlainTextResponse)
async def reset():
    """
    Reset cache timer
    """
    await app.redis_cache.set("predict_updated", -1)
    await app.redis_cache.set("current_bloom_updated", -1)
    await app.redis_cache.set("current_event_updated", -1)
    await app.redis_cache.set("cards_updated", -1)
    return "ok"


@app.get("/")
async def main() -> dict:
    """
    Shows API version
    """
    return {"version": APP_VERSION}


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_cache(app))
    loop.run_until_complete(update_database())
