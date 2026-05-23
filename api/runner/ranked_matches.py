"""
Ranked match collector (30m cron)
uses sbuga.com, redacted
"""

if __name__ == "__main__":
    collect_statistics()
    print("Done")
    mysql.close()
