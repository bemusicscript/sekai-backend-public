import time
import random
import orjson
import requests

def now():
    return int(time.time() * 1000)

def random_number(min_val, max_val):
    return random.uniform(min_val, max_val)

def download_data_from_jiiku():
    url = f"https://raw.githubusercontent.com/Jiiku831/Jiiku831.github.io/refs/heads/main/data/sekarun_current.json?t={now()}&j={random_number(0, 100000*100000)}"
    response = requests.get(url)
    response.raise_for_status()
    prediction_data = response.content

    with open('./data/sekarun.json', 'wb') as f:
        f.write(prediction_data)

    print("[downloadData] Download Cdomplete. (sekarun.json)")
    return orjson.loads(prediction_data)

def download_data_from_three():
    url = f"https://sekai-data.3-3.dev/predict.json?t={now()}"
    response = requests.get(url)
    response.raise_for_status()
    prediction_data = response.text

    with open('./data/33.json', 'w') as f:
        f.write(prediction_data)

    print("[downloadData] Download Complete. (33.json)")
    return orjson.loads(prediction_data)

def get_current_event_info():
    response = requests.get("http://api.internal:5000/current_event")
    response.raise_for_status()
    return orjson.loads(response.content)

def optimize_data(res, event_id=None):
    result = {}

    for rank, rank_info in res.items():
        if not event_id:
            result['eventId'] = rank_info['event_id']
        rank_numeric = str(rank)

        for rank_entry in rank_info['entries']:
            if rank_entry['entry_type'] == "h":
                continue
            if rank_numeric not in result:
                result[rank_numeric] = []
            result[rank_numeric].append([int(rank_entry['timestamp']), int(rank_entry['ep'])])

    return result

def init():
    event_info = get_current_event_info()
    final_data = {
        'eventId': event_info['id'],
        'jiiku': {
            'eventId': None,
            'all': {},
            'wl': {},
        },
        'three': {
            'eventId': None,
            'all': {},
            'wl': {},
        },
        'run': {
            'eventId': None,
            'all': {},
            'wl': {},
        },
    }

    try:
        prediction_data_jiiku = download_data_from_jiiku()
        final_data['jiiku']['all'] = optimize_data(prediction_data_jiiku['lines'])
        final_data['jiiku']['eventId'] = final_data['jiiku']['all'].pop('eventId', None)

        with open('../app/database/json/worldBlooms.json', 'rb') as f:
            database_event_list = orjson.loads(f.read())

        for chapter_no, chapter_info in prediction_data_jiiku['chapters'].items():
            match = next(
                (e for e in database_event_list
                 if e['eventId'] == final_data['jiiku']['eventId'] and str(e['chapterNo']) == str(chapter_no)),
                None
            )
            character_id = match['gameCharacterId']
            final_data['jiiku']['wl'][str(character_id)] = optimize_data(chapter_info['lines'], final_data['jiiku']['eventId'])

        print("[init] Jiiku parser done.")
        with open('./result.json', 'wb') as f:
            f.write(orjson.dumps(final_data))
    except Exception as e:
        print("[init] Jiiku prediction parser failed")
        print(e)

if __name__ == "__main__":
    init()