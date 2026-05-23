#!/usr/bin/python3 -u
#-*- coding: utf-8 -*-

import os
import sys
import time
import json
import datetime
import urllib.parse
import hashlib
import hmac
import base64
import redis

def reverse_readline(filename, buf_size=8192):
    """A generator that returns the lines of a file in reverse order"""
    with open(filename, 'rb') as fh:
        segment = None
        offset = 0
        fh.seek(0, os.SEEK_END)
        file_size = remaining_size = fh.tell()
        while remaining_size > 0:
            offset = min(file_size, offset + buf_size)
            fh.seek(file_size - offset)
            buffer = fh.read(min(remaining_size, buf_size))
            # remove file's last "\n" if it exists, only for the first buffer
            if remaining_size == file_size and buffer[-1] == ord('\n'):
                buffer = buffer[:-1]
            remaining_size -= buf_size
            lines = buffer.split('\n'.encode())
            # append last chunk's segment to this chunk's last line
            if segment is not None:
                lines[-1] += segment
            segment = lines[0]
            lines = lines[1:]
            # yield lines in this chunk except the segment
            for line in reversed(lines):
                # only decode on a parsed line, to avoid utf-8 decode error
                yield line.decode()
        # Don't yield None if the file was empty
        if segment is not None:
            yield segment.decode()

def main():
    result = {}
    now = time.time()
    for line in reverse_readline(sys.argv[1], 40960):
        try:
            log = json.loads(line.strip())
        except:
            continue

        ##### Parse Traffic
        ts = log['ts']
        if now - ts >= 600: break

        ts = datetime.datetime.fromtimestamp(ts)
        ip = log['request']['headers'].get('Cf-Connecting-Ip', [''])[0]
        ip_country = None
        if ip:
            ip_country = log['request']['headers'].get('Cf-Ipcountry', [''])[0]

        ua = log['request']['headers'].get('User-Agent', [''])[0]
        ref = log['request']['headers'].get('Referer', [''])[0]
        lang = log['request']['headers'].get('Accept-Language', [''])[0]
        host = log['request']['headers'].get('Host', [''])[0]
        uri = log['request']['uri']
        host = log['request']['host']
        method = log['request']['method']
        status = log['status']

        if ip not in result:
            result[ip] = {}

        ##### PROFILE MODE
        profile_data = ""
        profile_id = -1
        rank_id = -1

        if "&rank=" in uri:
            try:
                rank_id = uri.split("&rank=")[1].split("&")[0]
                profile_id = uri.split("?profile_id=")[1].split("&")[0]
                result[ip][profile_id] = {
                    'rank_id': rank_id,
                    'country': ip_country
                }
            except Exception as e:
                pass

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
        print("Authentication failed. Check your password.")
        return
    except redis.ConnectionError:
        print("Could not connect to Redis.")
        return

    r.set("ACTIVE_COUNT", len(result))
    r.set("ACTIVE_DATA", json.dumps(result)[:1024000])
    print(json.dumps(result, indent=4))

if __name__ == "__main__":
    main()
