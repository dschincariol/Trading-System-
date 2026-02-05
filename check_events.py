# check_events.py
import sqlite3

con = sqlite3.connect("dev.db")
try:
    total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    print("events_total =", total)

    rows = con.execute(
        """
        SELECT ts_ms, source, title, url
        FROM events
        ORDER BY ts_ms DESC
        LIMIT 15
        """
    ).fetchall()

    for ts_ms, source, title, url in rows:
        print(source, "::", title)
        if url:
            print("  ", url)
finally:
    con.close()
