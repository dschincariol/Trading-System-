# check_labels.py
import sqlite3


def main():
    with sqlite3.connect("dev.db") as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        total = con.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        print("labels_total =", total)

        rows = con.execute(
            "SELECT symbol, horizon_s, COUNT(*) FROM labels GROUP BY symbol, horizon_s ORDER BY symbol, horizon_s"
        ).fetchall()

        for sym, h, c in rows:
            print(f"{sym} horizon_s={h} count={c}")


if __name__ == '__main__':
    main()
