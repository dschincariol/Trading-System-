# check_events.py
import sqlite3


def main():
    # open connection on demand and set pragmas for WAL and performance
    with sqlite3.connect("dev.db") as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
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


if __name__ == '__main__':
    main()
