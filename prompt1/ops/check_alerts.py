# check_alerts.py
import sqlite3

from engine.alerts import SCHEMA as ALERTS_SCHEMA


def main():
    with sqlite3.connect("dev.db") as con:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        # Ensure the *authoritative* alerts schema (matches dev_core/alerts.py)
        con.executescript(ALERTS_SCHEMA)

        total = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        print("alerts_total =", total)

        rows = con.execute(
            """
            SELECT severity, rule_id, symbol, horizon_s, expected_z, confidence, event_title, explain_json
            FROM alerts
            ORDER BY ts_ms DESC
            LIMIT 25
            """
        ).fetchall()

        for sev, rule_id, sym, h, z, conf, title, explain_json in rows:
            explain = ""
            if explain_json:
                explain = " | explain_json=1"
            print(f"{sev} {rule_id} {sym} h={h} z={z:+.3f} conf={conf:.2f} :: {title}{explain}")


if __name__ == '__main__':
    main()
