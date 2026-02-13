from dev_core.storage import connect
import sys

def main():
    con = connect(readonly=True)
    try:
        orphan = con.execute(
            """
            SELECT COUNT(1)
            FROM pnl_attribution p
            LEFT JOIN trade_attribution_ledger t
              ON p.ts_ms = t.ts_ms
             AND p.source_alert_id = t.source_alert_id
             AND p.symbol = t.symbol
            WHERE t.id IS NULL
            """
        ).fetchone()[0]

        if int(orphan or 0) > 0:
            print(f"FAIL orphan_pnl_rows={orphan}")
            return 1

        print("OK trade attribution complete")
        return 0
    finally:
        con.close()

if __name__ == "__main__":
    raise SystemExit(main())
