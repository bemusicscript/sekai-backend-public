"""Event scoreboard collector (1-minute cron).

Collects live rankings from multiple sources into `scoreboard_current`.
When the event ends, archives the final round into `scoreboard_history` and collects profile honors into
`profile_honors` for later processing by honor.py.
"""

if __name__ == "__main__":
    update_data()
    print("Done")
    mysql.close()
