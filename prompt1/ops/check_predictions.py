# check_predictions.py
import sqlite3


def main():
    with sqlite3.connect("dev.db") as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        total = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        print("predictions_total =", total)

        rows = con.execute(
            """
            SELECT p.symbol, p.horizon_s, p.predicted_z, p.confidence, e.title
            FROM predictions p
            JOIN events e ON e.id = p.event_id
            ORDER BY p.ts_ms DESC
            LIMIT 20
            """
        ).fetchall()

        for sym, h, z, conf, title in rows:
            print(f"{sym} h={h} z={z:+.3f} conf={conf:.2f} :: {title}")


if __name__ == '__main__':
    main()
