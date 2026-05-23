"""Profile honor processor (20-minute cron).

Reads the `profile_honors` cache populated by runner_current.py, validates each
honor badge against scoreboard history to confirm event placements, and inserts
confirmed records into scoreboard tables. Truncates `profile_honors` on completion.
"""

import re
import os
import orjson

from utils import connect_db

CURRENT_PATH = os.path.dirname(os.path.realpath(__file__))
TMP_PATH = os.path.join(CURRENT_PATH, "tmp_honor")
DB_PATH = os.path.join(CURRENT_PATH, "../app/database/json/")

mysql = connect_db()


blooms = {}
with open(os.path.join(DB_PATH, "worldBlooms.json"), "rb") as f:
    tmp = orjson.loads(f.read())
    for v in tmp:
        if v['worldBloomChapterType'] == "finale":
            continue
        if not blooms.get(v['eventId']):
            blooms[v['eventId']] = {}
        blooms[v['eventId']][v['chapterNo']] = v['gameCharacterId']
del tmp

def generate_mapping() -> tuple[dict, dict]:
    """
    "name": "TOP100 SEEKER",
    "assetbundleName": "honor_top_000100_event_underwater_cp1",
    "levels": [
        "description": "イベント「水底に影を探して」のチャプター1で100位以内になろう。"
    """

    with open(os.path.join(DB_PATH, "honors.json"), "rb") as f:
        honors = orjson.loads(f.read())
    with open(os.path.join(DB_PATH, "events.json"), "rb") as f:
        events = orjson.loads(f.read())

    target_ranks = ['1位', '2位', '3位', '4位', '5位', '6位', '7位', '8位', '9位', '10位', 'TOP10', 'TOP20', 'TOP30', 'TOP40', 'TOP50', 'TOP100', 'TOP200', 'TOP300', 'TOP400', 'TOP500', 'TOP1000',
        'TOP100 SEEKER',
        'TOP100 REFLECT',
        'TOP100 STRUGGLE',
        'TOP100 EMBRACE'
    ]
    event_map = {}
    for event in events:
        event_map[event['name']] = event['id']

    honor_map = {}
    for honor in honors:
        if honor["name"] not in target_ranks:
            continue

        # Parse WL description
        honor_description = honor["levels"][0].get('description', None)
        if not honor_description:
            continue
        find_top = re.findall("イベント「(.+)」で([0-9]+)位になろう。", honor_description)
        find_nor = re.findall("イベント「(.+)」で([0-9]+)位以内になろう。", honor_description)
        find_wl = re.findall("イベント「(.+)」のチャプター(.+)で([0-9]+)位以内になろう。", honor_description)

        if find_top:
            found_type = "specific"
            honor_event_name = find_top[0][0]
            honor_event_rank = find_top[0][1]
            honor_chapter = 0
        elif find_nor:
            found_type = "range"
            honor_event_name = find_nor[0][0]
            honor_event_rank = find_nor[0][1]
            honor_chapter = 0
        elif find_wl:
            found_type = "wl"
            honor_event_name = find_wl[0][0]
            honor_event_rank = find_wl[0][2]
            honor_chapter = find_wl[0][1]
        else:
            print("[!] Critical: regex not found..")
            exit(0)

        honor_event_id = event_map.get(honor_event_name)
        if not honor_event_id:
            print(f"[!] Critical: event is not found ({honor_event_name})")
            continue

        # WL todo tho
        honor_map[honor['id']] = {
            "eid": honor_event_id,
            "rank": int(honor_event_rank),
            "type": found_type,
            "character": honor_chapter
        }
    return honor_map, event_map


def check_profile_data(honor_map: dict) -> None:
    cur = mysql.cursor()
    sql = "SELECT * FROM profile_honors"
    cur.execute(sql)
    result = cur.fetchall()
    if len(result) < 1:
        print("No data")
        return

    query_values_range = []

    for data in result:
        scoreboard_profile_id = data[1]
        scoreboard_honor = data[2]

        honor_info = honor_map.get(scoreboard_honor)
        if not honor_info:
            continue

        if honor_info['type'] == "specific":
            # find from existing scoreboard, check if not exists.
            sql = "SELECT * FROM scoreboard_history WHERE scoreboard_profile_id=? AND scoreboard_event_id=? AND scoreboard_rank=?"
            val = (scoreboard_profile_id, honor_info['eid'], honor_info['rank'])
            cur.execute(sql, val)
            result_history = cur.fetchall()

            # this covers anything from 113 to latest
            # if it already exists we skip.
            if len(result_history) > 0:
                continue

            # but if this data is something we already recorded, we need to keep memo
            # because our existing record is completely wrong.
            if honor_info['eid'] > 113:
                with open(os.path.join(TMP_PATH, "broken_data"), "ab") as f:
                    f.write(orjson.dumps(val) + "\n".encode())
                continue

            guessed_nickname = ""
            """
            # check if the nickname cache is in incorrect DB
            sql = "SELECT scoreboard_nickname FROM scoreboard_history_incorrect WHERE scoreboard_profile_id=? AND scoreboard_event_id=? LIMIT 1"
            val = (scoreboard_profile_id, honor_info['eid'])
            print(val)
            cur.execute(sql, val)
            result_incorrect = cur.fetchall()
            if len(result_incorrect) > 0:
                guessed_nickname = result_incorrect[0][0]
                print(f"Guessed: {guessed_nickname}")
            """

            # then new data's final score can be retrieved from 3-3.json
            graph_path = os.path.join(TMP_PATH, "prev_graphs", f"{honor_info['eid']}_{honor_info['rank']}.json")

            final_score = None
            try:
                with open(graph_path, "rb") as f:
                    existing_log = orjson.loads(f.read())
                final_score = existing_log[-1]['score']

            except Exception as e:
                print(f"[check_profile_data] prev_graphs open failed for eid={honor_info['eid']} rank={honor_info['rank']}: {e}")
                with open(os.path.join(TMP_PATH, "broken_final_score"), "ab") as f:
                    f.write(f"{orjson.dumps(val)} ({honor_info['rank']})\n".encode())
                # last resort. it has to work tbh.
                finals_path = os.path.join(TMP_PATH, "prev_finals", f"{honor_info['eid']}.json")
                try:
                    with open(finals_path, "rb") as f:
                        existing_log_final = orjson.loads(f.read())
                    for _r in existing_log_final:
                        _rank = _r[0]
                        _score = _r[1]
                        if _rank != honor_info['rank']:
                            continue
                        final_score = _r[1]
                        print("found on last resort", val, _rank, _score)
                        break
                    if not final_score:
                        raise Exception("WTF")

                except Exception as e:
                    print(f"[check_profile_data] prev_finals last resort failed for eid={honor_info['eid']}: {e}")
                    with open(os.path.join(TMP_PATH, "broken_final_last_resort"), "ab") as f:
                        f.write(f"{orjson.dumps(val)} ({honor_info['rank']})\n".encode())
                    print("failed on last resort :()")
                    final_score = 0

            sql = "INSERT IGNORE INTO scoreboard_history VALUES (?, ?, ?, ?, ?, ?, ?)"
            val = (
                None,
                honor_info['eid'],
                honor_info['rank'],
                scoreboard_profile_id,
                guessed_nickname,
                final_score,
                '0'
            )
            cur.execute(sql, val)
            continue

        # range based
        if honor_info['type'] == "range":
            # everything <= 1000
            if honor_info['rank'] >= 1000:
                continue

            # check if the user is already recorded on scoreboard_history
            # (sometimes T10 is also there so this is important)
            sql = "SELECT * FROM scoreboard_history WHERE scoreboard_profile_id=? AND scoreboard_event_id=?"
            val = (scoreboard_profile_id, honor_info['eid'])
            cur.execute(sql, val)
            result_existing = cur.fetchall()

            # if it already exists we skip.
            if len(result_existing) > 0:
                continue

            # add to range
            query_values_range.append((None, honor_info['eid'], honor_info['rank'], scoreboard_profile_id, None, 0))

        if honor_info['type'] == "wl":
            if honor_info['rank'] >= 1000:
                continue
            if honor_info['eid'] != 112:
                continue
            real_character_id = blooms[honor_info['eid']][int(honor_info['character'])]
            sql = "SELECT * FROM scoreboard_history WHERE scoreboard_profile_id=? AND scoreboard_event_id=? AND scoreboard_type=?"
            val = (scoreboard_profile_id, honor_info['eid'], real_character_id)
            cur.execute(sql, val)
            result_existing = cur.fetchall()

            # if it already exists we skip.
            if len(result_existing) > 0:
                continue

            # add to range
            query_values_range.append((None, honor_info['eid'], honor_info['rank'], scoreboard_profile_id, None, real_character_id))
            print(real_character_id, honor_info)

    if len(query_values_range) > 0:
        cur.executemany(
            "INSERT IGNORE INTO scoreboard_history_top VALUES (%s, %s, %s, %s, %s, %s)",
            query_values_range,
        )
        mysql.commit()

    sql = "TRUNCATE TABLE profile_honors;"
    cur.execute(sql)
    mysql.commit()


if __name__ == "__main__":
    honor_map, event_map = generate_mapping()
    check_profile_data(honor_map)
